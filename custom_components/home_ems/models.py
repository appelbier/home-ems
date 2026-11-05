"""Manual time slots and heavy loads.

Both can be one-off (start/end datetimes) or recurring (weekdays plus a
local start and end time). Heavy loads can also be *flexible*: give a window
and the planner picks the cheapest start.

Pure Python / numpy, no Home Assistant imports.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta, tzinfo
import math
import uuid

import numpy as np

from .parsing import parse_time
from .timeseries import SLOT, SLOT_SECONDS

WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def new_id() -> str:
    return uuid.uuid4().hex[:8]


def _time_str(value: time | None) -> str | None:
    return value.strftime("%H:%M") if value else None


def _parse_clock(value) -> time | None:
    if value is None or isinstance(value, time):
        return value
    parts = [int(p) for p in str(value).split(":")[:2]]
    return time(parts[0], parts[1] if len(parts) > 1 else 0)


def _occurrences(
    weekdays: Sequence[int], start: time, end: time, first: datetime, last: datetime, tz: tzinfo
) -> list[tuple[datetime, datetime]]:
    """Recurring local windows overlapping ``[first, last)``."""
    out = []
    day = (first.astimezone(tz) - timedelta(days=1)).date()
    stop = last.astimezone(tz).date()
    while day <= stop:
        if day.weekday() in weekdays:
            a = datetime.combine(day, start, tz)
            b = datetime.combine(day, end, tz)
            if b <= a:
                b += timedelta(days=1)
            if b > first and a < last:
                out.append((a, b))
        day += timedelta(days=1)
    return out


def _overlap_hours(slot_starts: Sequence[datetime], a: datetime, b: datetime) -> np.ndarray:
    """Hours of each slot covered by ``[a, b)``."""
    hours = np.zeros(len(slot_starts))
    if not slot_starts:
        return hours
    t0 = slot_starts[0].timestamp()
    sa, sb = a.timestamp(), b.timestamp()
    first = max(0, int((sa - t0) // SLOT_SECONDS))
    last = min(len(slot_starts), int(-(-(sb - t0) // SLOT_SECONDS)))
    for i in range(first, last):
        s = t0 + i * SLOT_SECONDS
        overlap = min(sb, s + SLOT_SECONDS) - max(sa, s)
        if overlap > 0:
            hours[i] = overlap / 3600
    return hours


@dataclass
class ManualSlot:
    """Forced battery mode for a period."""

    mode: str
    power_kw: float | None = None
    start: datetime | None = None
    end: datetime | None = None
    weekdays: list[int] = field(default_factory=list)
    start_time: time | None = None
    end_time: time | None = None
    name: str = ""
    id: str = field(default_factory=new_id)
    source: str = "service"  # "yaml" slots come from configuration.yaml

    def windows(self, first: datetime, last: datetime, tz: tzinfo) -> list[tuple[datetime, datetime]]:
        if self.start and self.end:
            return [(self.start, self.end)] if self.end > first and self.start < last else []
        if self.weekdays and self.start_time and self.end_time:
            return _occurrences(self.weekdays, self.start_time, self.end_time, first, last, tz)
        return []

    def mask(self, slot_starts: Sequence[datetime], tz: tzinfo, now: datetime | None = None) -> np.ndarray:
        """Slots where at least half the slot is inside the window.

        The first slot is usually partly over; with ``now`` it is active when
        the window covers the current moment.
        """
        covered = np.zeros(len(slot_starts))
        if not slot_starts:
            return covered.astype(bool)
        windows = self.windows(slot_starts[0], slot_starts[-1] + SLOT, tz)
        for a, b in windows:
            covered += _overlap_hours(slot_starts, a, b)
        active = covered >= SLOT_SECONDS / 3600 / 2
        if now is not None and slot_starts[0] <= now < slot_starts[0] + SLOT:
            active[0] = any(a <= now < b for a, b in windows)
        return active

    def expired(self, now: datetime) -> bool:
        return bool(self.end and not self.weekdays and self.end <= now)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["start"] = self.start.isoformat() if self.start else None
        data["end"] = self.end.isoformat() if self.end else None
        data["start_time"] = _time_str(self.start_time)
        data["end_time"] = _time_str(self.end_time)
        return data

    @classmethod
    def from_dict(cls, data: dict, tz: tzinfo) -> ManualSlot:
        return cls(
            mode=data["mode"],
            power_kw=data.get("power_kw"),
            start=parse_time(data.get("start"), tz),
            end=parse_time(data.get("end"), tz),
            weekdays=list(data.get("weekdays") or []),
            start_time=_parse_clock(data.get("start_time")),
            end_time=_parse_clock(data.get("end_time")),
            name=data.get("name") or "",
            id=data.get("id") or new_id(),
            source=data.get("source") or "service",
        )


@dataclass
class HeavyLoad:
    """A known extra consumer (heat pump boost, sauna, washing machine...)."""

    name: str
    power_kw: float
    duration_min: int
    start: datetime | None = None  # one-off fixed start
    weekdays: list[int] = field(default_factory=list)  # recurring
    start_time: time | None = None
    earliest: datetime | None = None  # flexible window
    latest: datetime | None = None  # must be finished by
    chosen_start: datetime | None = None  # planner's pick for flexible loads
    id: str = field(default_factory=new_id)
    source: str = "service"

    @property
    def flexible(self) -> bool:
        return self.earliest is not None and self.latest is not None

    @property
    def duration(self) -> timedelta:
        return timedelta(minutes=self.duration_min)

    def runs(self, first: datetime, last: datetime, tz: tzinfo) -> list[tuple[datetime, datetime]]:
        if self.flexible:
            start = self.chosen_start
            return [(start, start + self.duration)] if start else []
        if self.start:
            return [(self.start, self.start + self.duration)]
        if self.weekdays and self.start_time:
            end = (datetime.combine(datetime.min, self.start_time) + self.duration).time()
            return _occurrences(self.weekdays, self.start_time, end, first, last, tz)
        return []

    def profile(self, slot_starts: Sequence[datetime], tz: tzinfo, start: datetime | None = None) -> np.ndarray:
        """Energy per slot (kWh). ``start`` overrides the run start (flexible search)."""
        energy = np.zeros(len(slot_starts))
        if not slot_starts:
            return energy
        if start is not None:
            runs = [(start, start + self.duration)]
        else:
            runs = self.runs(slot_starts[0], slot_starts[-1] + SLOT, tz)
        for a, b in runs:
            energy += _overlap_hours(slot_starts, a, b) * self.power_kw
        return energy

    def candidate_starts(self, now: datetime, step: timedelta) -> list[datetime]:
        """Possible starts for a flexible load, ``step`` apart, all in the future."""
        if not self.flexible:
            return []
        first = max(self.earliest, now)
        ts = first.timestamp()
        rem = ts % step.total_seconds()
        if rem:
            first = first + timedelta(seconds=step.total_seconds() - rem)
        last = self.latest - self.duration
        out = []
        current = first
        while current <= last:
            out.append(current)
            current += step
        return out

    def choose_start(
        self,
        slot_starts: Sequence[datetime],
        tz: tzinfo,
        marginal_cost: np.ndarray,
        now: datetime,
        step: timedelta = SLOT,
    ) -> datetime | None:
        """Cheapest start inside the window, priced at the plan's marginal cost."""
        best, best_cost = None, math.inf
        if not slot_starts:
            return None
        horizon_end = slot_starts[-1] + SLOT
        for start in self.candidate_starts(now, step):
            if start + self.duration > horizon_end:
                break
            cost = float(np.dot(self.profile(slot_starts, tz, start), marginal_cost))
            if cost < best_cost - 1e-9:
                best, best_cost = start, cost
        return best

    def expired(self, now: datetime) -> bool:
        if self.weekdays:
            return False
        if self.flexible:
            return self.latest <= now
        return bool(self.start and self.start + self.duration <= now)

    def to_dict(self) -> dict:
        data = asdict(self)
        for key in ("start", "earliest", "latest", "chosen_start"):
            value = getattr(self, key)
            data[key] = value.isoformat() if value else None
        data["start_time"] = _time_str(self.start_time)
        return data

    @classmethod
    def from_dict(cls, data: dict, tz: tzinfo) -> HeavyLoad:
        return cls(
            name=data.get("name") or "load",
            power_kw=float(data["power_kw"]),
            duration_min=int(data["duration_min"]),
            start=parse_time(data.get("start"), tz),
            weekdays=list(data.get("weekdays") or []),
            start_time=_parse_clock(data.get("start_time")),
            earliest=parse_time(data.get("earliest"), tz),
            latest=parse_time(data.get("latest"), tz),
            chosen_start=parse_time(data.get("chosen_start"), tz),
            id=data.get("id") or new_id(),
            source=data.get("source") or "service",
        )
