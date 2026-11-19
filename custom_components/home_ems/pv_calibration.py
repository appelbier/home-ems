"""Learns how far the PV forecast is systematically off, per hour of the day.

Solcast is usually right about the weather but can be consistently off for a
given roof: a wrong kWp or tilt in its site settings, shade from a tree in the
morning, an inverter that clips at noon, dirty panels. Home EMS logs the P50
forecast it used for every quarter hour and compares it with the metered yield
of the last weeks:

- overall factor: a forecast-weighted median of the daily ratios, so a snowy
  day or an inverter outage does not drag it down, shrunk towards 1 while there
  are only a few days;
- hour factors: the ratio per local hour of the day (morning shade, evening
  sun), pulled towards the overall factor where an hour has little energy.

Factors stay within ``LIMITS``. Pure Python/numpy, no Home Assistant imports.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone, tzinfo

import numpy as np

DAYS = 21  # look-back
MIN_DAYS = 3  # below this the forecast is used as is
PRIOR_DAYS = 3.0  # the overall factor counts 1.0 as this many days
PRIOR_KWH = 2.0  # an hour with little energy leans on the overall factor
MIN_DAY_KWH = 1.0  # days with a tiny forecast say nothing
DAY_RATIO = (0.3, 3.0)  # daily ratios outside this are outages / snow, not bias
LIMITS = (0.5, 1.5)


def calibrate(forecast: Mapping[int, float], actual: Mapping[int, float], tz: tzinfo, today: date) -> dict:
    """Overall and per-hour factors (actual / forecast) from the slots that have both."""
    first = today - timedelta(days=DAYS)
    days: dict[date, list[float]] = {}
    hours: dict[date, np.ndarray] = {}
    for ts, fc in forecast.items():
        if ts not in actual:
            continue
        local = datetime.fromtimestamp(ts, timezone.utc).astimezone(tz)
        day = local.date()
        if not first <= day < today:
            continue
        sums = days.setdefault(day, [0.0, 0.0])
        sums[0] += fc
        sums[1] += actual[ts]
        per_hour = hours.setdefault(day, np.zeros((2, 24)))
        per_hour[0, local.hour] += fc
        per_hour[1, local.hour] += actual[ts]

    valid = [
        d for d, (fc, act) in days.items() if fc >= MIN_DAY_KWH and DAY_RATIO[0] <= act / fc <= DAY_RATIO[1]
    ]
    result = {"factor": 1.0, "hours": [1.0] * 24, "days": len(valid)}
    if len(valid) < MIN_DAYS:
        return result
    ratios = np.array([days[d][1] / days[d][0] for d in valid])
    weights = np.array([days[d][0] for d in valid])
    median = _weighted_median(ratios, weights)
    overall = (len(valid) * median + PRIOR_DAYS) / (len(valid) + PRIOR_DAYS)
    overall = float(np.clip(overall, *LIMITS))
    totals = sum(hours[d] for d in valid)
    per_hour = (totals[1] + PRIOR_KWH * overall) / (totals[0] + PRIOR_KWH)
    result.update(
        factor=round(overall, 3),
        hours=[round(float(np.clip(f, *LIMITS)), 3) for f in per_hour],
        forecast_kwh=round(float(totals[0].sum()), 1),
        actual_kwh=round(float(totals[1].sum()), 1),
    )
    return result


def slot_factors(calibration: Mapping, slot_starts: Sequence[datetime], tz: tzinfo) -> np.ndarray:
    hours = calibration.get("hours") or [1.0] * 24
    return np.array([hours[s.astimezone(tz).hour] for s in slot_starts])


def _weighted_median(values: np.ndarray, weights: np.ndarray) -> float:
    order = np.argsort(values)
    cum = np.cumsum(weights[order])
    return float(values[order][np.searchsorted(cum, cum[-1] / 2)])
