"""House load prediction from the recorded load history.

For each future day the model combines:

* the same weekday in the previous weeks (strongest signal, recent weeks count
  more), so a weekly rhythm like home-office days or a Saturday sauna shows up;
* the last few days, weighted by how similar they are (weekday vs weekend), so
  seasonal drift (heating, lighting in winter) is followed quickly;
* a robust per-slot average (weighted median, outliers beyond a few MADs are
  dropped) so a one-off party does not shift the forecast;
* a level correction: if the last two days ran higher or lower than the model
  would have predicted, the near future is scaled, fading out over ~1.5 days.

Pure numpy, no Home Assistant imports.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, tzinfo
import math

import numpy as np

from .timeseries import daily_matrix, slot_of_day

# Typical Dutch household shape per hour (relative weights), used until there
# is history: low at night, a morning bump, and an evening peak.
_DEFAULT_HOURLY_SHAPE = np.array(
    [
        0.55, 0.50, 0.48, 0.47, 0.47, 0.50,
        0.70, 1.05, 1.10, 0.95, 0.90, 0.92,
        0.98, 0.92, 0.88, 0.90, 1.05, 1.45,
        1.70, 1.65, 1.50, 1.35, 1.05, 0.75,
    ]
)

MIN_SLOTS_FOR_DAY = 80
LEVEL_CLIP = (0.8, 1.25)
LEVEL_FADE_HOURS = 36.0


def plausible_load(value: float, max_slot_kwh: float) -> bool:
    """False for readings no house can produce in 15 minutes.

    Energy meters that briefly read 0 (or a garbled value) look like a meter
    reset to the recorder, which then books the whole meter total in one hour.
    """
    return 0.0 <= value <= max_slot_kwh


def default_day_profile(daily_kwh: float) -> np.ndarray:
    """96 slot values summing to ``daily_kwh``."""
    shape = np.repeat(_DEFAULT_HOURLY_SHAPE, 4)
    return shape / shape.sum() * daily_kwh


def _smooth(day: np.ndarray) -> np.ndarray:
    """Light 3-tap smoothing; keeps peaks but removes 15 min jitter."""
    padded = np.concatenate(([day[0]], day, [day[-1]]))
    return 0.25 * padded[:-2] + 0.5 * padded[1:-1] + 0.25 * padded[2:]


def _fill_day(day: np.ndarray) -> np.ndarray:
    """Interpolate gaps inside a mostly complete day."""
    out = day.copy()
    bad = ~np.isfinite(out)
    if bad.any() and (~bad).any():
        idx = np.arange(len(out))
        out[bad] = np.interp(idx[bad], idx[~bad], out[~bad])
    return out


def _weighted_robust_mean(matrix: np.ndarray, weights: np.ndarray) -> np.ndarray:
    """Per-column weighted mean after dropping outliers (weighted median +/- 3 MAD)."""
    order = np.argsort(matrix, axis=0)
    sorted_vals = np.take_along_axis(matrix, order, axis=0)
    sorted_w = weights[order]
    cum = np.cumsum(sorted_w, axis=0)
    half = cum[-1] / 2
    pos = (cum >= half).argmax(axis=0)
    median = sorted_vals[pos, np.arange(matrix.shape[1])]
    deviation = np.abs(matrix - median)
    mad = np.median(deviation, axis=0)
    limit = 3 * np.maximum(mad, 0.05 * np.abs(median) + 0.01)
    keep = deviation <= limit
    w = weights[:, None] * keep
    total = w.sum(axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        result = (matrix * w).sum(axis=0) / total
    return np.where(total > 0, result, median)


class LoadForecaster:
    """Predict 15 minute house load (kWh) from history keyed by slot timestamp."""

    def __init__(
        self,
        history: Mapping[int, float],
        tz: tzinfo,
        default_daily_kwh: float = 15.0,
        weeks: int = 4,
        recent_days: int = 7,
        max_slot_kwh: float | None = None,
    ) -> None:
        self.tz = tz
        self.default_daily_kwh = default_daily_kwh
        self.weeks = weeks
        self.recent_days = recent_days
        if max_slot_kwh is not None:
            history = {k: v for k, v in history.items() if plausible_load(v, max_slot_kwh)}
        raw = daily_matrix(history, tz, agg="sum")
        self.days: dict[date, np.ndarray] = {
            day: _smooth(_fill_day(values))
            for day, values in raw.items()
            if np.isfinite(values).sum() >= MIN_SLOTS_FOR_DAY
        }
        self._cache: dict[tuple[date, date], np.ndarray] = {}

    def _candidates(self, target: date, today: date, exclude: date | None) -> tuple[list, list]:
        weights: dict[date, float] = {}
        for k in range(1, self.weeks + 1):
            day = target - timedelta(days=7 * k)
            if day < today and day != exclude and day in self.days:
                weights[day] = weights.get(day, 0.0) + 2.0 * 0.75 ** (k - 1)
        target_weekend = target.weekday() >= 5
        for j in range(1, self.recent_days + 1):
            day = today - timedelta(days=j)
            if day == exclude or day not in self.days:
                continue
            similar = (day.weekday() >= 5) == target_weekend
            weights[day] = weights.get(day, 0.0) + 0.6 ** (j - 1) * (1.0 if similar else 0.35)
        days = list(weights)
        return days, [weights[d] for d in days]

    def day_profile(self, target: date, today: date, exclude: date | None = None) -> np.ndarray:
        key = (target, today if exclude is None else exclude)
        if exclude is None and key in self._cache:
            return self._cache[key]
        days, weights = self._candidates(target, today, exclude)
        if not days:
            profile = default_day_profile(self.default_daily_kwh)
        else:
            matrix = np.vstack([self.days[d] for d in days])
            profile = _weighted_robust_mean(matrix, np.asarray(weights))
            profile = np.maximum(profile, 0.0)
        if exclude is None:
            self._cache[key] = profile
        return profile

    def level_factor(self, today: date) -> float:
        """How the last two days compare with what the model expected."""
        ratios = []
        for j in (1, 2):
            day = today - timedelta(days=j)
            if day not in self.days:
                continue
            expected = self.day_profile(day, day, exclude=day).sum()
            actual = self.days[day].sum()
            if expected > 0.1:
                ratios.append(actual / expected)
        if not ratios:
            return 1.0
        return float(np.clip(np.mean(ratios), *LEVEL_CLIP))

    def forecast(self, slot_starts: Sequence[datetime], now: datetime) -> np.ndarray:
        today = now.astimezone(self.tz).date()
        level = self.level_factor(today) if self.days else 1.0
        out = np.zeros(len(slot_starts))
        for i, start in enumerate(slot_starts):
            local = start.astimezone(self.tz)
            profile = self.day_profile(local.date(), today)
            hours_ahead = max(0.0, (start - now).total_seconds() / 3600)
            factor = 1.0 + (level - 1.0) * math.exp(-hours_ahead / LEVEL_FADE_HOURS)
            out[i] = profile[slot_of_day(local)] * factor
        return out

    @property
    def history_days(self) -> int:
        return len(self.days)
