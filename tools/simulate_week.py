"""Compare a 7-day plan with a short rolling horizon on a synthetic week.

    python tools/simulate_week.py

Prints the cost of: plain self consumption, a rolling 36 h planner (what
you get when only day-ahead prices are considered) and the 7-day planner,
plus a per-day breakdown of the week plan.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
import time

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scenario import TARIFF, TZ, build_week  # noqa: E402

from home_ems.optimizer import (  # noqa: E402
    BatteryConfig,
    GridConfig,
    PlanInput,
    optimise,
    simulate_self_consumption,
)
from home_ems.price_forecast import terminal_value  # noqa: E402

BAT = BatteryConfig(
    capacity_kwh=150,
    reserve_kwh=15,
    max_charge_kw=10,
    max_discharge_kw=10,
    charge_efficiency=0.95,
    discharge_efficiency=0.95,
    cycle_cost=0.01,
)
GRID = GridConfig(import_limit_kw=17, export_limit_kw=10)
START_SOC = 60.0


def make_input(sl, imp, exp, pv, load, soc, tv=None):
    n = sl.stop - sl.start
    return PlanInput(
        duration_h=np.full(n, 0.25),
        load_kwh=load[sl],
        pv_kwh=pv[sl],
        import_price=imp[sl],
        export_price=exp[sl],
        initial_soc_kwh=soc,
        terminal_value=terminal_value(imp[sl], BAT.charge_efficiency) if tv is None else tv,
    )


def rolling(imp, exp, pv, load, horizon_slots, total_slots):
    """Re-plan every hour with a limited horizon, execute one hour."""
    soc = START_SOC
    cost = 0.0
    for start in range(0, total_slots, 4):
        end = min(start + horizon_slots, len(imp))
        plan = optimise(make_input(slice(start, end), imp, exp, pv, load, soc), BAT, GRID)
        steps = min(4, total_slots - start)
        cost += plan.cost[:steps].sum()
        soc = plan.soc_kwh[steps]
    return cost, soc


def main() -> None:
    start = datetime(2026, 10, 29, 23, 0, tzinfo=timezone.utc)  # Friday 00:00 local
    slots, raw, pv, load = build_week(start, days=10)
    imp = TARIFF.import_price(raw)
    exp = TARIFF.export_price(raw)
    week = 7 * 96

    t0 = time.perf_counter()
    plan = optimise(make_input(slice(0, week), imp, exp, pv, load, START_SOC), BAT, GRID)
    solve_s = time.perf_counter() - t0
    base = simulate_self_consumption(make_input(slice(0, week), imp, exp, pv, load, START_SOC), BAT, GRID)

    # The week planner, executed hourly with a 7 day look-ahead (rolling).
    week_cost, week_soc = rolling(imp, exp, pv, load, 7 * 96, week)
    short_cost, short_soc = rolling(imp, exp, pv, load, 36 * 4, week)

    tv = terminal_value(imp[:week], BAT.charge_efficiency)
    print(f"7-day solve: {solve_s * 1000:.0f} ms for {week} slots")
    print(f"End-of-week energy valued at {tv:.3f} EUR/kWh\n")
    print(f"{'strategy':28s} {'cost EUR':>9s} {'end SoC':>8s} {'net EUR':>9s}")
    for name, cost, soc in (
        ("self consumption only", base.total_cost, base.soc_kwh[-1]),
        ("rolling 36 h horizon", short_cost, short_soc),
        ("rolling 7 day horizon", week_cost, week_soc),
    ):
        net = cost - (soc - START_SOC) * tv
        print(f"{name:28s} {cost:9.2f} {soc:8.1f} {net:9.2f}")

    print("\n7-day plan per day:")
    print(f"{'day':10s} {'avg imp':>8s} {'grid in':>8s} {'grid out':>8s} {'charge':>7s} {'soc end':>8s} {'cost':>7s}")
    for d in range(7):
        sl = slice(d * 96, (d + 1) * 96)
        day = slots[d * 96].astimezone(TZ).strftime("%a %d")
        charge = sum(1 for m in plan.mode[sl] if m == "charge") / 4
        print(
            f"{day:10s} {imp[sl].mean():8.3f} {np.maximum(plan.grid_kwh[sl], 0).sum():8.1f} "
            f"{-np.minimum(plan.grid_kwh[sl], 0).sum():8.1f} {charge:6.1f}h {plan.soc_kwh[(d + 1) * 96]:8.1f} "
            f"{plan.cost[sl].sum():7.2f}"
        )


if __name__ == "__main__":
    main()
