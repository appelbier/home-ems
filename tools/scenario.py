"""Synthetic but realistic week used by the tests and the simulation script.

150 kWh battery, 6.4 kWp PV in late autumn, ~15 kWh/day house load, Dutch
day-ahead prices that are cheap in the weekend and expensive on weekdays.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import math
from pathlib import Path
import sys
import types
from zoneinfo import ZoneInfo

import numpy as np

PACKAGE_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "home_ems"
if "home_ems" not in sys.modules:
    package = types.ModuleType("home_ems")
    package.__path__ = [str(PACKAGE_DIR)]
    sys.modules["home_ems"] = package

from home_ems.load_forecast import default_day_profile  # noqa: E402
from home_ems.price_forecast import Tariff  # noqa: E402
from home_ems.timeseries import SLOT, slot_of_day  # noqa: E402

TZ = ZoneInfo("Europe/Amsterdam")


def raw_price(local: datetime, rng: np.random.Generator) -> float:
    """EUR/kWh day-ahead price with a weekday/weekend split and a solar dip."""
    h = local.hour + local.minute / 60
    weekend = local.weekday() >= 5
    if weekend:
        base = 0.055
        morning = 0.015 * math.exp(-((h - 8.5) ** 2) / 2)
        evening = 0.03 * math.exp(-((h - 19) ** 2) / 3)
        solar = -0.06 * math.exp(-((h - 13) ** 2) / 4) if local.weekday() == 6 else -0.03 * math.exp(-((h - 13) ** 2) / 4)
    else:
        base = 0.105
        morning = 0.06 * math.exp(-((h - 8) ** 2) / 2)
        evening = 0.11 * math.exp(-((h - 18.75) ** 2) / 3)
        solar = -0.025 * math.exp(-((h - 13) ** 2) / 5)
    return base + morning + evening + solar + rng.normal(0, 0.006)


def pv_kw(local: datetime, day_factor: float, kwp: float = 6.4) -> float:
    """Average PV power for late October in the Netherlands."""
    h = local.hour + local.minute / 60
    sunrise, sunset = 8.2, 17.4
    if h <= sunrise or h >= sunset:
        return 0.0
    x = (h - sunrise) / (sunset - sunrise)
    return kwp * 0.55 * math.sin(math.pi * x) ** 1.5 * day_factor


def build_week(start: datetime, days: int = 7, seed: int = 1):
    rng = np.random.default_rng(seed)
    slots = [start + SLOT * i for i in range(days * 96)]
    day_factors = [0.9, 0.35, 0.6, 0.2, 0.8, 0.5, 0.7, 0.4, 0.9, 0.3]
    raw = np.array([raw_price(s.astimezone(TZ), rng) for s in slots])
    pv = np.array(
        [pv_kw(s.astimezone(TZ), day_factors[(s.astimezone(TZ).date() - start.astimezone(TZ).date()).days % 10]) * 0.25 for s in slots]
    )
    profile = default_day_profile(15.0)
    load = np.array([profile[slot_of_day(s.astimezone(TZ))] for s in slots])
    load = load * rng.uniform(0.85, 1.15, len(load))
    return slots, raw, pv, load


def load_history(end: datetime, days: int = 28, seed: int = 2) -> dict[int, float]:
    """History with a weekday rhythm: higher load on Wednesday and weekends."""
    rng = np.random.default_rng(seed)
    profile = default_day_profile(15.0)
    history = {}
    t = end - timedelta(days=days)
    while t < end:
        local = t.astimezone(TZ)
        factor = {2: 1.3, 5: 1.2, 6: 1.25}.get(local.weekday(), 1.0)
        history[int(t.timestamp())] = profile[slot_of_day(local)] * factor * rng.uniform(0.8, 1.2)
        t += SLOT
    return history


TARIFF = Tariff(vat=0.21, energy_tax=0.0916, import_markup=0.02, export_mode="raw", export_fee=0.01)
