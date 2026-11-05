"""Electricity price handling: tariff formulas, calibration and a fallback forecast.

Prices are stored internally as *raw* day-ahead prices in EUR/kWh (EPEX, no
tax). Consumer import and export prices are computed from the raw price with
the configured tariff, so sources that already include tax (e.g. a forecast
site that shows all-in prices) can be mixed with raw day-ahead data.

Pure numpy, no Home Assistant imports.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
import math

import numpy as np

from .timeseries import daily_matrix, slot_of_day

EXPORT_RAW = "raw"
EXPORT_SAME_AS_IMPORT = "import"


@dataclass
class Tariff:
    """How raw day-ahead prices become what you actually pay / receive."""

    vat: float = 0.21
    energy_tax: float = 0.0916  # EUR/kWh excl. VAT
    import_markup: float = 0.02  # supplier fee EUR/kWh excl. VAT
    export_mode: str = EXPORT_RAW
    export_fee: float = 0.0  # subtracted from raw price for export, excl. VAT
    export_vat: bool = False

    def import_price(self, raw: np.ndarray) -> np.ndarray:
        return (raw + self.import_markup + self.energy_tax) * (1 + self.vat)

    def export_price(self, raw: np.ndarray) -> np.ndarray:
        if self.export_mode == EXPORT_SAME_AS_IMPORT:
            return self.import_price(raw)
        price = raw - self.export_fee
        return price * (1 + self.vat) if self.export_vat else price

    def to_raw(self, all_in: np.ndarray | float) -> np.ndarray | float:
        """Invert ``import_price`` for sources that publish all-in prices."""
        return all_in / (1 + self.vat) - self.energy_tax - self.import_markup


UNIT_FACTORS = {"eur_kwh": 1.0, "eur_mwh": 0.001, "ct_kwh": 0.01}


def unit_factor(
    unit: str, values: Sequence[float], unit_hint: str | None = None, all_in: bool = False
) -> float:
    """Factor to EUR/kWh. ``auto`` uses the unit string, then the magnitude.

    Large numbers are read as EUR/MWh for raw day-ahead prices and as cents
    for all-in consumer prices (which are never 2+ EUR/kWh).
    """
    if unit in UNIT_FACTORS:
        return UNIT_FACTORS[unit]
    hint = (unit_hint or "").lower()
    if "mwh" in hint:
        return 0.001
    if "ct" in hint or "cent" in hint:
        return 0.01
    if "kwh" in hint:
        return 1.0
    finite = [abs(v) for v in values if v is not None and math.isfinite(v)]
    if finite and float(np.median(finite)) > 2.0:
        return 0.01 if all_in else 0.001
    return 1.0


def fit_calibration(forecast: np.ndarray, actual: np.ndarray) -> tuple[float, float]:
    """Linear map ``actual ~ a * forecast + b`` fitted on overlapping slots.

    Used to line up a forecast source with the real day-ahead prices (its tax
    assumptions, supplier fee and systematic bias all end up in ``a``/``b``).
    """
    mask = np.isfinite(forecast) & np.isfinite(actual)
    n = int(mask.sum())
    if n < 4:
        return 1.0, 0.0
    f = forecast[mask]
    y = actual[mask]
    if n < 32 or float(np.std(f)) < 1e-6:
        return 1.0, float(np.mean(y - f))
    a, b = np.polyfit(f, y, 1)
    a = float(np.clip(a, 0.6, 1.4))
    b = float(np.mean(y - a * f))
    return a, b


def forecast_from_history(
    history: Mapping[int, float],
    slot_starts: Sequence[datetime],
    tz: tzinfo,
    now: datetime,
    weeks: int = 4,
) -> np.ndarray:
    """Fallback price forecast from recorded day-ahead prices.

    Same weekday and time in recent weeks (newer weeks weigh more), shifted
    towards the price level of the last three days. Captures the weekly
    pattern (cheap weekends, expensive weekdays) and the daily solar dip.
    Returns NaN where nothing can be said (no history).
    """
    days = daily_matrix(history, tz, agg="mean")
    out = np.full(len(slot_starts), np.nan)
    if not days:
        return out
    today = now.astimezone(tz).date()
    past = sorted(d for d in days if d <= today)
    if not past:
        return out

    overall = np.nanmean(np.vstack([days[d] for d in past]), axis=0)

    def base_profile(target: date, upto: date) -> np.ndarray | None:
        """Weekday/time profile from days strictly before ``upto``."""
        rows, weights = [], []
        for k in range(1, weeks + 1):
            day = target - timedelta(days=7 * k)
            if day in days and day < upto:
                rows.append(days[day])
                weights.append(1.0 - 0.2 * (k - 1))
        if not rows:
            weekend = target.weekday() >= 5
            for day in [d for d in past if d < upto][-14:]:
                if (day.weekday() >= 5) == weekend:
                    rows.append(days[day])
                    weights.append(1.0)
        if not rows:
            return None
        matrix = np.vstack(rows)
        w = np.asarray(weights)[:, None] * np.isfinite(matrix)
        with np.errstate(invalid="ignore", divide="ignore"):
            prof = np.nansum(np.nan_to_num(matrix) * w, axis=0) / w.sum(axis=0)
        return np.where(np.isfinite(prof), prof, overall)

    # Level drift: how the last days differ from what their own weekday
    # profile (built from older weeks) predicted.
    drifts = []
    for day in past[-3:]:
        ref = base_profile(day, day)
        if ref is not None:
            diff = days[day] - ref
            if np.isfinite(diff).any():
                drifts.append(float(np.nanmean(diff)))
    drift = float(np.mean(drifts)) if drifts else 0.0

    profiles: dict[date, np.ndarray] = {}

    def profile_for(target: date) -> np.ndarray:
        if target not in profiles:
            prof = base_profile(target, today + timedelta(days=1))
            if prof is None:
                prof = overall.copy()
            days_ahead = max(0, (target - today).days)
            profiles[target] = prof + 0.7 * drift * math.exp(-days_ahead / 5)
        return profiles[target]

    for i, start in enumerate(slot_starts):
        local = start.astimezone(tz)
        out[i] = profile_for(local.date())[slot_of_day(local)]
    return out


def terminal_value(import_price: np.ndarray, charge_efficiency: float, quantile: float = 15) -> float:
    """Default worth of 1 kWh left in the battery at the end of the horizon.

    Roughly what it would cost to buy that kWh back at a cheap moment. A low
    quantile keeps the plan from topping up just before the horizon ends.
    """
    finite = import_price[np.isfinite(import_price)]
    if not len(finite):
        return 0.0
    return max(0.0, float(np.percentile(finite, quantile)) / charge_efficiency)
