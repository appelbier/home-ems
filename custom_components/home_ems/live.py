"""Live power: a short time-weighted average and how it corrects the next hours.

The load forecast says what a quarter hour usually uses. When the house
draws far more right now (an oven, a heat pump defrost, a visitor's car), the
plan should know: in hold mode all of it would otherwise come from the grid at
whatever the price is. The live average replaces the forecast for the current
slot and fades back to the forecast over the next hour or so.

Pure Python, no Home Assistant imports.
"""

from __future__ import annotations

from collections import deque

import numpy as np

WINDOW_S = 300.0  # average over the last 5 minutes: a kettle should not flip the plan
FADE_HOURS = 0.75  # weight of the live value: 1 now, ~0.26 one hour ahead


class LiveAverage:
    """Time-weighted mean of a power reading over the last ``window_s`` seconds."""

    def __init__(self, window_s: float = WINDOW_S) -> None:
        self.window_s = window_s
        self.samples: deque[tuple[float, float]] = deque()

    def add(self, ts: float, value: float) -> None:
        self.samples.append((ts, value))
        # Keep one sample from before the window: it holds until the next one.
        while len(self.samples) > 1 and self.samples[1][0] <= ts - self.window_s:
            self.samples.popleft()

    def mean(self, now: float) -> float | None:
        if not self.samples:
            return None
        start = now - self.window_s
        total = 0.0
        covered = 0.0
        for i, (ts, value) in enumerate(self.samples):
            end = self.samples[i + 1][0] if i + 1 < len(self.samples) else now
            begin = max(ts, start)
            if end > begin:
                total += value * (end - begin)
                covered += end - begin
        if covered <= 0:
            return self.samples[-1][1]
        return total / covered


def blend_live(forecast_kwh: np.ndarray, hours_ahead: np.ndarray, live_kw: float, slot_hours: float) -> np.ndarray:
    """Forecast per slot pulled towards the live power, fading with the time ahead."""
    weight = np.exp(-np.maximum(hours_ahead, 0.0) / FADE_HOURS)
    return forecast_kwh + (live_kw * slot_hours - forecast_kwh) * weight


def to_kw(value: float, unit: str | None) -> float:
    """Power reading in kW: W and kW by unit, otherwise large numbers are read as W."""
    unit = (unit or "").strip().lower()
    if unit == "kw":
        return value
    if unit == "w" or (not unit and abs(value) > 100):
        return value / 1000
    return value
