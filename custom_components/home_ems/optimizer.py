"""Battery schedule optimiser.

Dynamic programming over a discretised state-of-charge grid (the value
function is interpolated between grid points). For every slot and every
possible SoC the backward pass finds the cheapest way to reach the end of the
horizon. This is globally optimal for the given forecast, which is what a
large (150 kWh) battery needs: the week as a whole decides whether to fill up
on a cheap Sunday and run on it until Friday.

Each slot offers only actions an inverter can actually execute:

* self consumption: absorb PV surplus / cover the house load (follows the
  load exactly, independent of the SoC grid);
* hold: battery idle (PV surplus may still charge it when forced);
* charge: grid charge at a fixed power level;
* export: discharge to the grid at a fixed power level.

Pure numpy, no Home Assistant imports.

Energy convention, per slot, in kWh:
    battery > 0   energy stored in the battery (DC side), < 0 drawn from it
    grid    > 0   import from the grid, < 0 export
    grid = load - pv + ac, where ac = battery / eta_c when charging
                                 ac = battery * eta_d when discharging
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math

import numpy as np

MODE_SELF_CONSUMPTION = "self_consumption"
MODE_CHARGE = "charge"
MODE_EXPORT = "export"
MODE_HOLD = "hold"
MODES = (MODE_SELF_CONSUMPTION, MODE_CHARGE, MODE_EXPORT, MODE_HOLD)

# Penalties keep soft constraints soft so the problem never becomes
# infeasible (e.g. a forced charge slot while the battery is already full).
FORCE_PENALTY = 5.0  # EUR per kWh short of a forced charge/export
SOC_PENALTY = 2.0  # EUR per kWh per slot below reserve or above max SoC
IMPORT_LIMIT_PENALTY = 5.0  # EUR per kWh above the grid connection limit
TIE_BREAK = 1e-4  # EUR per kWh, prefers natural behaviour when costs tie
MAX_TRADE_HURDLE = 0.20  # EUR per kWh at 1 % aggressiveness
SELF_CONSUMPTION_HURDLE = 10.0  # at 0 %: never deviate from self consumption


@dataclass
class BatteryConfig:
    """Physical battery and inverter limits."""

    capacity_kwh: float
    reserve_kwh: float = 0.0
    max_soc_kwh: float | None = None
    max_charge_kw: float = 5.0
    max_discharge_kw: float = 5.0  # battery -> house (self consumption)
    max_export_kw: float | None = None  # forced export setpoint; None = max_discharge_kw
    charge_efficiency: float = 0.95
    discharge_efficiency: float = 0.95
    cycle_cost: float = 0.0  # EUR per kWh moved in or out (wear)
    # Soft floor: every kWh below hold_kwh costs hold_cost per day, so the
    # battery only dips under it when the price makes that worth it, and is
    # topped up again when that is cheap.
    hold_kwh: float = 0.0
    hold_cost: float = 0.0  # EUR per kWh per day below hold_kwh
    step_kwh: float = 0.25

    @property
    def max_kwh(self) -> float:
        if self.max_soc_kwh is None:
            return self.capacity_kwh
        return min(self.max_soc_kwh, self.capacity_kwh)


@dataclass
class GridConfig:
    """Grid connection limits."""

    import_limit_kw: float = 17.0
    export_limit_kw: float = 17.0
    allow_curtail: bool = True  # inverter can stop exporting at negative prices


@dataclass
class PlanInput:
    """Forecast for every slot of the horizon."""

    duration_h: np.ndarray
    load_kwh: np.ndarray
    pv_kwh: np.ndarray
    import_price: np.ndarray
    export_price: np.ndarray
    initial_soc_kwh: float
    terminal_value: float = 0.0  # EUR per kWh left in the battery at the end
    allow_grid_charge: np.ndarray | None = None
    allow_battery_export: np.ndarray | None = None
    protected_load_kwh: np.ndarray | None = None  # load the battery may not feed
    force_mode: list[str | None] | None = None
    force_power_kw: np.ndarray | None = None
    # EUR per kWh the battery does differently from plain self consumption
    # (grid charging, export, holding back): the gain a trade must beat.
    trade_hurdle: float = 0.0

    def __post_init__(self) -> None:
        self.duration_h = np.asarray(self.duration_h, dtype=float)
        n = len(self.duration_h)
        self.load_kwh = np.asarray(self.load_kwh, dtype=float)
        self.pv_kwh = np.asarray(self.pv_kwh, dtype=float)
        self.import_price = np.asarray(self.import_price, dtype=float)
        self.export_price = np.asarray(self.export_price, dtype=float)
        if self.allow_grid_charge is None:
            self.allow_grid_charge = np.ones(n, dtype=bool)
        if self.allow_battery_export is None:
            self.allow_battery_export = np.ones(n, dtype=bool)
        if self.protected_load_kwh is None:
            self.protected_load_kwh = np.zeros(n)
        if self.force_mode is None:
            self.force_mode = [None] * n
        if self.force_power_kw is None:
            self.force_power_kw = np.full(n, np.nan)
        self.allow_grid_charge = np.asarray(self.allow_grid_charge, dtype=bool)
        self.allow_battery_export = np.asarray(self.allow_battery_export, dtype=bool)
        self.protected_load_kwh = np.asarray(self.protected_load_kwh, dtype=float)
        self.force_power_kw = np.asarray(self.force_power_kw, dtype=float)

    @property
    def slots(self) -> int:
        return len(self.duration_h)


@dataclass
class PlanResult:
    """Optimised schedule."""

    soc_kwh: np.ndarray  # SoC at the start of each slot, plus the final SoC
    battery_kwh: np.ndarray
    grid_kwh: np.ndarray
    cost: np.ndarray  # EUR per slot, energy only
    mode: list[str]
    power_kw: np.ndarray  # battery-side kW for charge/export modes, else 0
    curtail: np.ndarray
    total_cost: float
    final_value: float  # terminal value of the energy left at the end
    extra: dict = field(default_factory=dict)


def trade_hurdle(aggressiveness_pct: float) -> float:
    """Gain per kWh a trade must make: 0 at 100 %, 0.20 near 0 %, none at 0 %."""
    share = min(max(aggressiveness_pct / 100, 0.0), 1.0)
    return SELF_CONSUMPTION_HURDLE if share <= 0 else MAX_TRADE_HURDLE * (1 - share)


def _natural_nominal(net: float, dur: float, bat: BatteryConfig) -> float:
    """Self consumption ignoring SoC limits: absorb surplus, cover deficit."""
    if net < 0:
        return min(-net * bat.charge_efficiency, bat.max_charge_kw * dur)
    return -min(net / bat.discharge_efficiency, bat.max_discharge_kw * dur)


def natural_battery(net: float, dur: float, bat: BatteryConfig, soc: float) -> float:
    """What plain self consumption would do to the battery in one slot."""
    energy = _natural_nominal(net, dur, bat)
    low = min(0.0, bat.reserve_kwh - soc)
    high = max(0.0, bat.max_kwh - soc)
    return float(min(max(energy, low), high))


def slot_cost(
    battery: np.ndarray,
    net: float,
    dur: float,
    import_price: float,
    export_price: float,
    grid: GridConfig,
    bat: BatteryConfig,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Energy cost for a vector of battery moves in one slot.

    Returns ``(cost, grid_kwh, curtailed)``.
    """
    ac = np.where(battery > 0, battery / bat.charge_efficiency, battery * bat.discharge_efficiency)
    flow = net + ac
    imported = np.maximum(flow, 0.0)
    exported = np.minimum(np.maximum(-flow, 0.0), grid.export_limit_kw * dur)
    curtailed = np.zeros(np.shape(battery), dtype=bool)
    if export_price < 0 and grid.allow_curtail:
        curtailed = exported > 0
        exported = np.zeros_like(exported)
    cost = imported * import_price - exported * export_price
    over = np.maximum(imported - grid.import_limit_kw * dur, 0.0)
    cost = cost + over * IMPORT_LIMIT_PENALTY
    grid_flow = np.where(flow < 0, -exported, flow)
    return cost, grid_flow, curtailed


@dataclass
class _Actions:
    """Executable choices for one slot.

    ``moves`` are fixed battery energies (grid charge levels, export levels,
    idle). Self consumption is a separate, state-dependent action because it
    follows the load exactly, which a coarse SoC grid cannot represent.
    """

    net: float
    moves: np.ndarray
    kinds: list[str]
    penalty: np.ndarray
    natural: float | None
    natural_kind: str
    natural_penalty: float = 0.0
    hurdle: float = 0.0  # EUR per kWh of ``moves`` away from self consumption


def _slot_actions(inp: PlanInput, t: int, bat: BatteryConfig) -> _Actions:
    step = bat.step_kwh
    dur = float(inp.duration_h[t])
    net = float(inp.load_kwh[t] - inp.pv_kwh[t])
    eps = 1e-9
    max_c = bat.max_charge_kw * dur
    max_d = min(bat.max_discharge_kw, bat.max_export_kw or bat.max_discharge_kw) * dur
    nat = _natural_nominal(net, dur, bat)
    upper, lower = max(nat, 0.0), min(nat, 0.0)
    charge_levels = [k * step for k in range(1, math.floor(max_c / step + eps) + 1)]
    export_levels = [-k * step for k in range(1, math.floor(max_d / step + eps) + 1)]
    # Idle while PV is producing a surplus means feeding that PV to the grid.
    idle_kind = MODE_HOLD if nat < 0 else (MODE_EXPORT if nat > 0 else MODE_SELF_CONSUMPTION)

    force = inp.force_mode[t]
    power = float(inp.force_power_kw[t])
    if force == MODE_CHARGE:
        target = min(max_c, power * dur) if math.isfinite(power) else max_c
        moves = [0.0] + charge_levels
        natural = nat if nat > 0 else None
        pen = [FORCE_PENALTY * max(0.0, target - m) for m in moves]
        nat_pen = FORCE_PENALTY * max(0.0, target - nat) if natural is not None else 0.0
        return _Actions(net, np.array(moves), [MODE_CHARGE] * len(moves), np.array(pen), natural, MODE_CHARGE, nat_pen)
    if force == MODE_EXPORT:
        target = min(max_d, power * dur) if math.isfinite(power) else max_d
        moves = [0.0] + export_levels
        natural = nat if nat < 0 else None
        pen = [FORCE_PENALTY * max(0.0, target + m) for m in moves]
        nat_pen = FORCE_PENALTY * max(0.0, target + nat) if natural is not None else 0.0
        return _Actions(net, np.array(moves), [MODE_EXPORT] * len(moves), np.array(pen), natural, MODE_EXPORT, nat_pen)
    if force == MODE_HOLD:
        natural = nat if nat > 0 else None
        return _Actions(net, np.array([0.0]), [MODE_HOLD], np.zeros(1), natural, MODE_HOLD)
    if force == MODE_SELF_CONSUMPTION:
        return _Actions(net, np.zeros(0), [], np.zeros(0), nat, MODE_SELF_CONSUMPTION)

    protected = float(inp.protected_load_kwh[t])
    moves = [0.0]
    kinds = [idle_kind]
    if inp.allow_grid_charge[t]:
        for m in charge_levels:
            if m > upper + eps:
                moves.append(m)
                kinds.append(MODE_CHARGE)
    if inp.allow_battery_export[t] and protected <= 0:
        for m in export_levels:
            if m < lower - eps:
                moves.append(m)
                kinds.append(MODE_EXPORT)
    # The battery may not feed a protected load (e.g. the EV): no self
    # consumption discharge in those slots, only idle or charging.
    natural = None if (protected > 0 and nat < 0) else nat
    moves_arr = np.array(moves)
    penalty = TIE_BREAK * np.abs(moves_arr - nat)
    return _Actions(net, moves_arr, kinds, penalty, natural, MODE_SELF_CONSUMPTION, hurdle=inp.trade_hurdle)


def _evaluate(
    acts: _Actions,
    socs: np.ndarray,
    value_next: np.ndarray,
    levels: np.ndarray,
    inp: PlanInput,
    t: int,
    bat: BatteryConfig,
    grid: GridConfig,
) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Total cost (slot + future) of every action from every SoC in ``socs``.

    Returns ``(table, energy, kinds)``; rows are actions, columns states.
    """
    dur = float(inp.duration_h[t])
    imp = float(inp.import_price[t])
    exp = float(inp.export_price[t])
    top = levels[-1]
    rows: list[np.ndarray] = []
    energies: list[np.ndarray] = []
    kinds: list[str] = []
    low = np.minimum(0.0, bat.reserve_kwh - socs)
    high = np.maximum(0.0, bat.max_kwh - socs)
    if len(acts.moves):
        cost, _, _ = slot_cost(acts.moves, acts.net, dur, imp, exp, grid, bat)
        cost = cost + acts.penalty + bat.cycle_cost * np.abs(acts.moves)
        nxt = socs[None, :] + acts.moves[:, None]
        future = np.interp(nxt, levels, value_next)
        future[(nxt < -1e-9) | (nxt > top + 1e-9)] = np.inf
        row = cost[:, None] + future
        if acts.hurdle:
            # Measured against what self consumption could do from each SoC.
            reference = np.clip(acts.natural, low, high) if acts.natural is not None else np.zeros_like(socs)
            row = row + acts.hurdle * np.abs(acts.moves[:, None] - reference[None, :])
        rows.append(row)
        energies.append(np.broadcast_to(acts.moves[:, None], nxt.shape))
        kinds.extend(acts.kinds)
    if acts.natural is not None:
        energy = np.clip(acts.natural, low, high)
        cost, _, _ = slot_cost(energy, acts.net, dur, imp, exp, grid, bat)
        cost = cost + acts.natural_penalty + bat.cycle_cost * np.abs(energy)
        rows.append((cost + np.interp(socs + energy, levels, value_next))[None, :])
        energies.append(energy[None, :])
        kinds.append(acts.natural_kind)
    return np.vstack(rows), np.vstack(energies), kinds


def optimise(inp: PlanInput, bat: BatteryConfig, grid: GridConfig) -> PlanResult:
    """Find the cheapest battery schedule over the whole horizon."""
    slots = inp.slots
    step = bat.step_kwh
    states = int(round(bat.capacity_kwh / step)) + 1
    levels = np.arange(states) * step
    state_penalty = SOC_PENALTY * (
        np.maximum(bat.reserve_kwh - levels, 0.0) + np.maximum(levels - bat.max_kwh, 0.0)
    )

    hold_per_hour = np.maximum(bat.hold_kwh - levels, 0.0) * bat.hold_cost / 24

    values = np.empty((slots + 1, states))
    values[slots] = -inp.terminal_value * levels + state_penalty + hold_per_hour * float(inp.duration_h[-1])
    actions = [_slot_actions(inp, t, bat) for t in range(slots)]
    for t in range(slots - 1, -1, -1):
        table, _, _ = _evaluate(actions[t], levels, values[t + 1], levels, inp, t, bat, grid)
        values[t] = table.min(axis=0) + state_penalty + hold_per_hour * float(inp.duration_h[t])

    return _forward(inp, bat, grid, actions, values, levels)


def _forward(
    inp: PlanInput,
    bat: BatteryConfig,
    grid: GridConfig,
    actions: list[_Actions],
    values: np.ndarray,
    levels: np.ndarray,
) -> PlanResult:
    slots = inp.slots
    soc = float(min(max(inp.initial_soc_kwh, 0.0), bat.capacity_kwh))

    soc_out = np.zeros(slots + 1)
    battery_out = np.zeros(slots)
    grid_out = np.zeros(slots)
    cost_out = np.zeros(slots)
    power_out = np.zeros(slots)
    curtail_out = np.zeros(slots, dtype=bool)
    modes: list[str] = []
    soc_out[0] = soc

    for t in range(slots):
        acts = actions[t]
        table, energy_table, kinds = _evaluate(
            acts, np.array([soc]), values[t + 1], levels, inp, t, bat, grid
        )
        best = int(table[:, 0].argmin())
        energy = float(energy_table[best, 0])
        energy = min(max(energy, -soc), bat.capacity_kwh - soc)
        dur = float(inp.duration_h[t])
        cost, flow, curtailed = slot_cost(
            np.array([energy]),
            acts.net,
            dur,
            float(inp.import_price[t]),
            float(inp.export_price[t]),
            grid,
            bat,
        )
        mode = inp.force_mode[t] or kinds[best]
        if mode == MODE_CHARGE:
            power_out[t] = max(energy, 0.0) / dur if dur > 0 else 0.0
        elif mode == MODE_EXPORT:
            power_out[t] = max(-energy, 0.0) / dur if dur > 0 else 0.0

        modes.append(mode)
        battery_out[t] = energy
        grid_out[t] = flow[0]
        cost_out[t] = cost[0]
        curtail_out[t] = bool(curtailed[0])
        soc += energy
        soc_out[t + 1] = soc

    result = PlanResult(
        soc_kwh=soc_out,
        battery_kwh=battery_out,
        grid_kwh=grid_out,
        cost=cost_out,
        mode=modes,
        power_kw=power_out,
        curtail=curtail_out,
        total_cost=float(cost_out.sum()),
        final_value=float(inp.terminal_value * soc),
    )
    result.extra["marginal_cost"] = _marginal_cost(inp, bat, result, values, levels)
    return result


def _marginal_cost(
    inp: PlanInput, bat: BatteryConfig, plan: PlanResult, values: np.ndarray, levels: np.ndarray
) -> np.ndarray:
    """EUR per extra kWh of load in each slot, given the optimised plan.

    Importing slots cost the import price, exporting slots the lost export
    revenue, and slots balanced by the battery the value of stored energy
    (slope of the cost-to-go function). Used to place flexible loads and the
    EV without re-solving the whole problem for every candidate.
    """
    slots = inp.slots
    out = np.zeros(slots)
    step = levels[1] - levels[0]
    for t in range(slots):
        grid_kwh = plan.grid_kwh[t]
        if grid_kwh > 1e-6:
            out[t] = inp.import_price[t]
            continue
        if grid_kwh < -1e-6:
            out[t] = 0.0 if plan.curtail[t] else inp.export_price[t]
            continue
        soc = plan.soc_kwh[t + 1]
        lo = max(soc - step, 0.0)
        hi = min(soc + step, levels[-1])
        if hi - lo < 1e-9:
            out[t] = inp.import_price[t]
            continue
        slope = (np.interp(hi, levels, values[t + 1]) - np.interp(lo, levels, values[t + 1])) / (hi - lo)
        worth = max(0.0, -slope)  # EUR per kWh stored
        if plan.battery_kwh[t] > 0:
            out[t] = worth * bat.charge_efficiency
        else:
            out[t] = worth / bat.discharge_efficiency
        if plan.curtail[t]:
            out[t] = 0.0
    return out


def simulate_self_consumption(inp: PlanInput, bat: BatteryConfig, grid: GridConfig) -> PlanResult:
    """Baseline: the inverter's own self-consumption mode, no planning."""
    slots = inp.slots
    soc = float(inp.initial_soc_kwh)
    soc_out = np.zeros(slots + 1)
    battery_out = np.zeros(slots)
    grid_out = np.zeros(slots)
    cost_out = np.zeros(slots)
    soc_out[0] = soc
    for t in range(slots):
        dur = inp.duration_h[t]
        net = inp.load_kwh[t] - inp.pv_kwh[t]
        energy = natural_battery(net, dur, bat, soc)
        cost, flow, _ = slot_cost(
            np.array([energy]), net, dur, inp.import_price[t], inp.export_price[t], grid, bat
        )
        battery_out[t] = energy
        grid_out[t] = flow[0]
        cost_out[t] = cost[0]
        soc += energy
        soc_out[t + 1] = soc
    return PlanResult(
        soc_kwh=soc_out,
        battery_kwh=battery_out,
        grid_kwh=grid_out,
        cost=cost_out,
        mode=[MODE_SELF_CONSUMPTION] * slots,
        power_kw=np.zeros(slots),
        curtail=np.zeros(slots, dtype=bool),
        total_cost=float(cost_out.sum()),
        final_value=float(inp.terminal_value * soc),
    )


def plan_value(result: PlanResult, bat: BatteryConfig) -> float:
    """Net cost including wear and the value of energy left at the end."""
    wear = bat.cycle_cost * float(np.abs(result.battery_kwh).sum())
    return result.total_cost + wear - result.final_value


def simulate_no_battery(inp: PlanInput, grid: GridConfig, bat: BatteryConfig) -> float:
    """Energy cost of the same horizon without a battery."""
    total = 0.0
    for t in range(inp.slots):
        cost, _, _ = slot_cost(
            np.zeros(1), float(inp.load_kwh[t] - inp.pv_kwh[t]), float(inp.duration_h[t]),
            float(inp.import_price[t]), float(inp.export_price[t]), grid, bat,
        )
        total += float(cost[0])
    return total


def compare_plan(plan: PlanResult, inp: PlanInput, bat: BatteryConfig, grid: GridConfig) -> dict[str, float]:
    """What the plan saves versus plain self consumption and versus no battery.

    Both are net of battery wear and count the change in stored energy at the
    terminal value: energy bought now and still in the battery at the end of the
    horizon is not a loss, and energy that was already in it is not free.
    """
    baseline = simulate_self_consumption(inp, bat, grid)
    start_value = inp.terminal_value * inp.initial_soc_kwh
    plan_net = plan_value(plan, bat) + start_value
    no_battery = simulate_no_battery(inp, grid, bat)
    return {
        "baseline_cost": baseline.total_cost,
        "savings": plan_value(baseline, bat) + start_value - plan_net,
        "no_battery_cost": no_battery,
        "battery_savings": no_battery - plan_net,
        "wear_cost": bat.cycle_cost * float(np.abs(plan.battery_kwh).sum()),
        "stored_value_change": plan.final_value - start_value,
    }
