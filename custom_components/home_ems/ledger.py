"""Realised cost and savings from metered energy, per local day.

The plan's cost is a forecast for the coming week and moves with every
re-plan. This ledger looks back instead: for every past 15 minute slot it
takes the metered grid import/export, house load and PV yield, prices them
with the day-ahead price of that slot, and compares the real cost with what
the same load and PV would have cost without a battery (load minus PV straight
from or to the grid, capped at the export limit).

The difference is what the battery really saved, including its round-trip
losses and the inverter's own use. It is not net of wear: that is reported
separately as an estimate (``cycle_cost`` x energy through the battery).

Pure Python/numpy, no Home Assistant imports.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone, tzinfo

from .timeseries import SLOT_SECONDS

FIELDS = ("actual_cost", "no_battery_cost", "savings", "wear", "import_kwh", "export_kwh", "load_kwh", "pv_kwh", "battery_kwh")


def slot_costs(
    energy: tuple[float, float, float, float],
    prices: tuple[float, float],
    export_limit_kwh: float,
    allow_curtail: bool,
) -> tuple[float, float, float]:
    """``(actual_cost, no_battery_cost, battery_kwh)`` for one slot.

    ``energy`` is (grid import, grid export, load, PV) in kWh, ``prices`` the
    import and export price in EUR/kWh.
    """
    imported, exported, load, pv = energy
    import_price, export_price = prices
    actual = imported * import_price - exported * export_price
    net = load - pv
    if net >= 0:
        without = net * import_price
    else:
        fed_in = min(-net, export_limit_kwh)
        if export_price < 0 and allow_curtail:
            fed_in = 0.0
        without = -fed_in * export_price
    # What the battery took from (+) or gave to (-) the AC side.
    battery = (imported - exported) - net
    return actual, without, abs(battery)


def day_ledger(
    energy: Mapping[int, tuple[float, float, float, float]],
    prices: Mapping[int, tuple[float, float]],
    tz: tzinfo,
    export_limit_kw: float,
    allow_curtail: bool = True,
    cycle_cost: float = 0.0,
    max_kwh: tuple[float, float, float, float] | None = None,
) -> dict[str, dict[str, float]]:
    """Totals per local date (ISO string) over the slots that have energy and a price.

    ``max_kwh`` caps (import, export, load, PV) per slot: a slot with a reading
    above it, or below zero, is a meter glitch (a counter that jumps or resets)
    and is left out; it is counted in ``rejected``.
    """
    days: dict[str, dict[str, float]] = {}
    limit = export_limit_kw * SLOT_SECONDS / 3600
    for ts, values in energy.items():
        price = prices.get(ts)
        if price is None:
            continue
        day = datetime.fromtimestamp(ts, timezone.utc).astimezone(tz).date().isoformat()
        d = days.setdefault(day, {key: 0.0 for key in FIELDS} | {"slots": 0, "rejected": 0})
        if any(v < -0.01 for v in values) or (max_kwh and any(v > m for v, m in zip(values, max_kwh))):
            d["rejected"] += 1
            continue
        actual, without, battery = slot_costs(values, price, limit, allow_curtail)
        d["actual_cost"] += actual
        d["no_battery_cost"] += without
        d["savings"] += without - actual
        d["wear"] += cycle_cost * battery
        d["import_kwh"] += values[0]
        d["export_kwh"] += values[1]
        d["load_kwh"] += values[2]
        d["pv_kwh"] += values[3]
        d["battery_kwh"] += battery
        d["slots"] += 1
    return {
        day: {k: (int(v) if k in ("slots", "rejected") else round(v, 4)) for k, v in d.items()}
        for day, d in days.items()
    }


def merge(ledger: dict[str, dict], fresh: Mapping[str, dict]) -> None:
    """Add freshly computed days; a recomputation that covers fewer slots never replaces a fuller one."""

    def covered(record: Mapping) -> int:
        return record.get("slots", 0) + record.get("rejected", 0)

    for day, record in fresh.items():
        old = ledger.get(day)
        if old is None or covered(record) >= covered(old):
            ledger[day] = record


def summary(ledger: Mapping[str, dict], today: str) -> dict:
    """Totals for today, the last 7 and 30 days and everything recorded."""
    def total(days: list[str]) -> dict[str, float]:
        out = {key: round(sum(ledger[d].get(key, 0.0) for d in days), 2) for key in ("savings", "actual_cost", "no_battery_cost", "wear")}
        out["days"] = len(days)
        out["rejected_slots"] = sum(ledger[d].get("rejected", 0) for d in days)
        return out

    ordered = sorted(d for d in ledger if d <= today)
    day0 = date.fromisoformat(today)

    def since(n: int) -> list[str]:
        return [d for d in ordered if date.fromisoformat(d) > day0 - timedelta(days=n)]

    return {
        "since": ordered[0] if ordered else None,
        "today": total([d for d in ordered if d == today]),
        "last_7_days": total(since(7)),
        "last_30_days": total(since(30)),
        "total": total(ordered),
        "days": [{"date": d, **{k: ledger[d][k] for k in ("savings", "actual_cost", "no_battery_cost", "import_kwh", "export_kwh", "slots", "rejected") if k in ledger[d]}} for d in ordered[-31:]],
    }
