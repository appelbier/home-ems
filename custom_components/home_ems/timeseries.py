"""Time-slot helpers shared by the forecasters and the planner.

Everything here is pure Python / numpy (no Home Assistant imports) so it can be
unit tested on its own. Slots are 15 minutes, aligned to the UTC epoch, and
identified by the UTC timestamp (int seconds) of their start.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone, tzinfo
import math

import numpy as np

SLOT_SECONDS = 900
SLOT = timedelta(seconds=SLOT_SECONDS)
SLOT_HOURS = SLOT_SECONDS / 3600
SLOTS_PER_DAY = 96


def floor_slot(moment: datetime) -> datetime:
    """Return the start of the 15 minute slot containing ``moment`` (UTC)."""
    ts = int(moment.timestamp())
    return datetime.fromtimestamp(ts - ts % SLOT_SECONDS, timezone.utc)


def slot_grid(start: datetime, count: int) -> list[datetime]:
    """Return ``count`` consecutive slot starts beginning at ``start``."""
    first = floor_slot(start)
    return [first + SLOT * i for i in range(count)]


def slot_of_day(local: datetime) -> int:
    """Index 0..95 of a local wall-clock time within its day."""
    return local.hour * 4 + local.minute // 15


def daily_matrix(
    history: Mapping[int, float], tz: tzinfo, agg: str = "sum"
) -> dict[date, np.ndarray]:
    """Group a slot history into one 96-value array per local date.

    ``agg`` decides what happens when two slots share a wall-clock position
    (the repeated hour when DST ends): ``sum`` for energy, ``mean`` for prices.
    Missing slots are NaN.
    """
    sums: dict[date, np.ndarray] = {}
    counts: dict[date, np.ndarray] = {}
    for ts, value in history.items():
        if value is None or not math.isfinite(value):
            continue
        local = datetime.fromtimestamp(int(ts), tz)
        day = local.date()
        if day not in sums:
            sums[day] = np.zeros(SLOTS_PER_DAY)
            counts[day] = np.zeros(SLOTS_PER_DAY)
        idx = slot_of_day(local)
        sums[day][idx] += value
        counts[day][idx] += 1
    result: dict[date, np.ndarray] = {}
    for day, total in sums.items():
        cnt = counts[day]
        with np.errstate(invalid="ignore", divide="ignore"):
            arr = total / cnt if agg == "mean" else total.copy()
        arr[cnt == 0] = np.nan
        result[day] = arr
    return result


def points_to_slots(
    points: Iterable[tuple[datetime, datetime | None, float]],
    slot_starts: Sequence[datetime],
) -> np.ndarray:
    """Step-lookup: each slot takes the value of the point covering its start.

    Points are ``(start, end, value)``; a missing end means "until the next
    point" (capped at 2 hours), and for the last point the same spacing as
    the one before it. Hourly prices therefore fill four slots, quarter-hour
    prices one. Uncovered slots are NaN.
    """
    out = np.full(len(slot_starts), np.nan)
    if not slot_starts:
        return out
    t0 = int(slot_starts[0].timestamp())
    count = len(slot_starts)
    pts = sorted(points, key=lambda p: p[0])
    for i, (start, end, value) in enumerate(pts):
        if value is None or not math.isfinite(value):
            continue
        if end is None:
            if i + 1 < len(pts):
                end = min(pts[i + 1][0], start + timedelta(hours=2))
            elif i > 0:
                end = start + min(start - pts[i - 1][0], timedelta(hours=2))
            else:
                end = start + timedelta(hours=1)
        a = int(start.timestamp())
        b = int(end.timestamp())
        first = max(0, math.ceil((a - t0) / SLOT_SECONDS))
        last = min(count, math.ceil((b - t0) / SLOT_SECONDS))
        if first < last:
            out[first:last] = value
    return out


def expand_points(points: Sequence[tuple[datetime, datetime | None, float]]) -> dict[int, float]:
    """Points -> ``{slot_ts: value}`` for every 15 minute slot they cover."""
    if not points:
        return {}
    first = floor_slot(min(p[0] for p in points))
    last = max((p[1] or p[0] + timedelta(hours=1)) for p in points)
    count = max(1, math.ceil((last - first).total_seconds() / SLOT_SECONDS))
    starts = slot_grid(first, count)
    values = points_to_slots(points, starts)
    return {int(s.timestamp()): float(v) for s, v in zip(starts, values) if math.isfinite(v)}


def integrate_to_slots(
    points: Iterable[tuple[datetime, datetime, float]],
    slot_starts: Sequence[datetime],
) -> tuple[np.ndarray, np.ndarray]:
    """Integrate average power points (kW) into energy per slot (kWh).

    Returns ``(energy_kwh, covered_hours)`` so callers can see which slots had
    no data at all.
    """
    count = len(slot_starts)
    energy = np.zeros(count)
    covered = np.zeros(count)
    if not count:
        return energy, covered
    t0 = int(slot_starts[0].timestamp())
    for start, end, power in points:
        if power is None or not math.isfinite(power):
            continue
        a = start.timestamp()
        b = end.timestamp()
        if b <= a:
            continue
        first = max(0, int((a - t0) // SLOT_SECONDS))
        last = min(count, math.ceil((b - t0) / SLOT_SECONDS))
        for idx in range(first, last):
            s = t0 + idx * SLOT_SECONDS
            overlap = min(b, s + SLOT_SECONDS) - max(a, s)
            if overlap > 0:
                hours = overlap / 3600
                energy[idx] += power * hours
                covered[idx] += hours
    return energy, covered


def fill_by_time_of_day(
    values: np.ndarray, slot_starts: Sequence[datetime], tz: tzinfo, scale: float = 1.0
) -> np.ndarray:
    """Fill NaN slots with the mean of known slots at the same time of day."""
    out = values.copy()
    missing = ~np.isfinite(out)
    if not missing.any():
        return out
    sums = np.zeros(SLOTS_PER_DAY)
    cnt = np.zeros(SLOTS_PER_DAY)
    idx = np.array([slot_of_day(s.astimezone(tz)) for s in slot_starts])
    for i in np.flatnonzero(~missing):
        sums[idx[i]] += out[i]
        cnt[idx[i]] += 1
    with np.errstate(invalid="ignore", divide="ignore"):
        profile = sums / cnt
    for i in np.flatnonzero(missing):
        value = profile[idx[i]]
        if math.isfinite(value):
            out[i] = value * scale
    return out
