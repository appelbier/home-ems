"""Learns how far each price forecast can be trusted, per day ahead.

Every update records the forecast price used for each future hour, per source
and per lead day (0 = less than 24 h ahead, 1 = 24-48 h, ...). When the real
day-ahead price for that hour comes in, the pair is added to exponentially
weighted statistics (half-life two weeks). From those, per source and lead day:

- trust: how much of the predicted swing around the horizon mean really
  happens (regression slope, clipped to 0..1). An optimiser picks the peaks
  and dips of a noisy forecast, exactly where it is most wrong, so swings are
  damped by this factor before planning.
- bias: the average error left after the source calibration.
- error: the spread of what is left, which sets the forecast price margin.

Until a source has enough history a prior is used: trust falls by
``TRUST_PRIOR_STEP`` per day ahead. The history model and "repeat" fill are
averages already and start at full trust.

Pure numpy, no Home Assistant imports.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import astuple, dataclass

import numpy as np

LEAD_DAYS = 8
HALF_LIFE_SAMPLES = 24 * 14  # hourly samples per lead day: two weeks
PRIOR_SAMPLES = 96  # weight of the prior, in samples
TRUST_PRIOR_STEP = 0.07
MIN_TRUST_PRIOR = 0.4
ERROR_PRIOR = (0.015, 0.008)  # raw EUR/kWh: at lead day 0, extra per day
MAX_BIAS = 0.05
MARGIN_FACTOR = 0.5  # margin = this x error
PENDING_DAYS = 2  # forget predictions whose real price never arrived
UNTRACKED = ("day_ahead", "")
AVERAGED = ("history", "repeat")
DECAY = 0.5 ** (1 / HALF_LIFE_SAMPLES)


@dataclass
class _Stats:
    """Weighted sums for ``y ~ c + b * x`` (x: predicted, y: real deviation)."""

    n: float = 0.0
    sx: float = 0.0
    sy: float = 0.0
    sxx: float = 0.0
    sxy: float = 0.0
    syy: float = 0.0

    def add(self, x: float, y: float) -> None:
        self.n = self.n * DECAY + 1
        self.sx = self.sx * DECAY + x
        self.sy = self.sy * DECAY + y
        self.sxx = self.sxx * DECAY + x * x
        self.sxy = self.sxy * DECAY + x * y
        self.syy = self.syy * DECAY + y * y


@dataclass(frozen=True)
class Skill:
    trust: float
    bias: float
    error: float
    samples: float


class ForecastSkill:
    def __init__(self) -> None:
        self.stats: dict[str, list[_Stats]] = {}
        # hour timestamp -> lead day -> (predicted raw price, horizon mean, origin)
        self.pending: dict[int, dict[int, tuple[float, float, str]]] = {}

    # ---------------------------------------------------------------- learning

    def observe(
        self,
        now_ts: float,
        slot_ts: np.ndarray,
        raw: np.ndarray,
        firm: np.ndarray,
        origin: np.ndarray,
        actual: Mapping[int, float],
    ) -> None:
        """Score predictions whose real price is now known, then record new ones."""
        for ts in list(self.pending):
            if ts in actual:
                for lead, (pred, mean, source) in self.pending.pop(ts).items():
                    self._stats(source)[lead].add(pred - mean, actual[ts] - mean)
            elif ts < now_ts - PENDING_DAYS * 86400:
                del self.pending[ts]
        mean = float(np.mean(raw))
        for i in np.flatnonzero(~firm):
            ts = int(slot_ts[i])
            if ts % 3600 or origin[i] in UNTRACKED:
                continue
            lead = int((ts - now_ts) // 86400)
            if 0 <= lead < LEAD_DAYS:
                self.pending.setdefault(ts, {})[lead] = (float(raw[i]), mean, str(origin[i]))

    def _stats(self, source: str) -> list[_Stats]:
        return self.stats.setdefault(source, [_Stats() for _ in range(LEAD_DAYS)])

    # --------------------------------------------------------------- applying

    def skill(self, source: str, lead: int) -> Skill:
        lead = min(max(lead, 0), LEAD_DAYS - 1)
        prior_trust = 1.0 if source in AVERAGED else max(MIN_TRUST_PRIOR, 1 - TRUST_PRIOR_STEP * lead)
        prior_error = ERROR_PRIOR[0] + ERROR_PRIOR[1] * lead
        s = self.stats[source][lead] if source in self.stats else _Stats()
        if s.n < 2:
            return Skill(prior_trust, 0.0, prior_error, s.n)
        mx, my = s.sx / s.n, s.sy / s.n
        var_x = s.sxx / s.n - mx * mx
        var_y = max(s.syy / s.n - my * my, 0.0)
        cov = s.sxy / s.n - mx * my
        slope = cov / var_x if var_x > 1e-6 else prior_trust
        resid = max(var_y - slope * cov, 0.0) if var_x > 1e-6 else var_y
        w = s.n / (s.n + PRIOR_SAMPLES)
        trust = float(np.clip(w * slope + (1 - w) * prior_trust, 0.0, 1.0))
        bias = float(np.clip(w * (my - slope * mx), -MAX_BIAS, MAX_BIAS))
        error = float(np.sqrt(w * resid + (1 - w) * prior_error**2))
        return Skill(trust, bias, error, s.n)

    def adjust(
        self, now_ts: float, slot_ts: np.ndarray, raw: np.ndarray, firm: np.ndarray, origin: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        """Damped forecast prices and the price margin per slot (raw EUR/kWh)."""
        mean = float(np.mean(raw))
        out = raw.copy()
        margin = np.zeros(len(raw))
        for i in np.flatnonzero(~firm):
            if origin[i] in UNTRACKED:
                continue
            k = self.skill(str(origin[i]), int((slot_ts[i] - now_ts) // 86400))
            out[i] = mean + k.bias + k.trust * (raw[i] - mean)
            margin[i] = MARGIN_FACTOR * k.error
        return out, margin

    def summary(self, sources: Iterable[str]) -> dict[str, list[dict]]:
        names = [s for s in dict.fromkeys([*sources, *self.stats]) if s not in UNTRACKED]
        return {
            name: [
                {
                    "days_ahead": lead,
                    "trust": round(k.trust, 2),
                    "bias": round(k.bias, 4),
                    "error": round(k.error, 4),
                    "samples": round(k.samples),
                }
                for lead in range(LEAD_DAYS)
                for k in [self.skill(name, lead)]
            ]
            for name in names
        }

    # ------------------------------------------------------------ persistence

    def to_dict(self) -> dict:
        return {
            "stats": {name: [[round(v, 6) for v in astuple(s)] for s in rows] for name, rows in self.stats.items()},
            "pending": {
                str(ts): {str(lead): [round(p, 5), round(m, 5), o] for lead, (p, m, o) in leads.items()}
                for ts, leads in self.pending.items()
            },
        }

    @classmethod
    def from_dict(cls, data: Mapping | None) -> ForecastSkill:
        skill = cls()
        if not data:
            return skill
        for name, rows in (data.get("stats") or {}).items():
            stats = [_Stats(*map(float, row)) for row in rows[:LEAD_DAYS]]
            skill.stats[name] = stats + [_Stats() for _ in range(LEAD_DAYS - len(stats))]
        for ts, leads in (data.get("pending") or {}).items():
            skill.pending[int(ts)] = {int(lead): (float(p), float(m), str(o)) for lead, (p, m, o) in leads.items()}
        return skill
