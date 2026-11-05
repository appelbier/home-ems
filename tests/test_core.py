"""Tests for the Home Assistant independent core of the EMS."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from home_ems.forecast_skill import ForecastSkill
from home_ems.ev_planner import EV_MAX, EV_SMART, EV_SOLAR, EvRequest, plan_ev
from home_ems.load_forecast import LoadForecaster
from home_ems.models import HeavyLoad, ManualSlot
from home_ems.optimizer import (
    MODE_CHARGE,
    MODE_EXPORT,
    MODE_HOLD,
    BatteryConfig,
    GridConfig,
    PlanInput,
    compare_plan,
    optimise,
    simulate_no_battery,
    simulate_self_consumption,
    trade_hurdle,
)
from home_ems.parsing import extract_series, parse_csv, solcast_points
from home_ems.price_forecast import Tariff, fit_calibration, forecast_from_history
from home_ems.timeseries import SLOT, integrate_to_slots, points_to_slots, slot_grid

TZ = ZoneInfo("Europe/Amsterdam")
UTC = timezone.utc
BAT = BatteryConfig(capacity_kwh=20, reserve_kwh=2, max_charge_kw=4, max_discharge_kw=4, cycle_cost=0.0)
GRID = GridConfig(import_limit_kw=17, export_limit_kw=10)


def plan_input(n, load=0.25, pv=0.0, imp=0.3, exp=0.05, soc=10.0, **kw):
    full = lambda v: np.full(n, v, dtype=float) if np.isscalar(v) else np.asarray(v, dtype=float)
    return PlanInput(
        duration_h=np.full(n, 0.25),
        load_kwh=full(load),
        pv_kwh=full(pv),
        import_price=full(imp),
        export_price=full(exp),
        initial_soc_kwh=soc,
        **kw,
    )


# --- parsing ---------------------------------------------------------------


def test_parse_nordpool_raw_attributes():
    attrs = {
        "unit": "kWh",
        "today": [0.1, 0.2],
        "raw_today": [
            {"start": "2026-10-30T00:00:00+01:00", "end": "2026-10-30T00:15:00+01:00", "value": 0.101},
            {"start": "2026-10-30T00:15:00+01:00", "end": "2026-10-30T00:30:00+01:00", "value": 0.102},
        ],
        "raw_tomorrow": [
            {"start": "2026-10-31T00:00:00+01:00", "end": "2026-10-31T00:15:00+01:00", "value": 0.05},
        ],
    }
    pts = extract_series(attrs, TZ)
    assert [p[2] for p in pts] == [0.101, 0.102, 0.05]
    assert pts[0][1] - pts[0][0] == timedelta(minutes=15)


def test_parse_entsoe_and_service_shapes():
    entsoe = {"prices": [{"time": "2026-10-30 10:00:00+01:00", "price": 0.12}, {"time": "2026-10-30 11:00:00+01:00", "price": 0.13}]}
    assert len(extract_series(entsoe, TZ)) == 2
    energyzero = {"prices": [{"timestamp": "2026-10-30T09:00:00+00:00", "price": 0.2}, {"timestamp": "2026-10-30T10:00:00+00:00", "price": 0.21}]}
    assert extract_series(energyzero, TZ)[1][2] == 0.21
    nordpool_service = {"NL": [{"start": "2026-10-30T09:00:00+00:00", "end": "2026-10-30T09:15:00+00:00", "price": 95.3}]}
    assert extract_series(nordpool_service, TZ)[0][2] == 95.3
    epoch = [[1793350800000, 0.1], [1793354400000, 0.2]]
    assert extract_series(epoch, TZ)[0][0].year == 2026
    epex_predictor = {
        "prices": [{"startsAt": "2026-10-31T11:30:00+01:00", "total": 0.2177}, {"startsAt": "2026-10-31T11:45:00+01:00", "total": 0.2143}],
        "knownUntil": "2026-10-31T23:45:00+01:00",
    }
    pts = extract_series(epex_predictor, TZ)
    assert [p[2] for p in pts] == [0.2177, 0.2143]
    assert pts[1][0] - pts[0][0] == timedelta(minutes=15)


def test_parse_csv_and_solcast():
    pts = parse_csv("time;price\n2026-10-30T10:00:00Z;0,25\n2026-10-30T11:00:00Z;0,30\n", TZ)
    assert [p[2] for p in pts] == [0.25, 0.30]
    api = [{"pv_estimate": 2.0, "pv_estimate10": 1.0, "pv_estimate90": 3.0, "period_end": "2026-10-30T10:30:00.0000000Z", "period": "PT30M"}]
    start, end, p50, p10, p90 = solcast_points(api, TZ)[0]
    assert end - start == timedelta(minutes=30) and (p50, p10, p90) == (2.0, 1.0, 3.0)
    integration = [{"period_start": "2026-10-30T10:00:00+01:00", "pv_estimate": 1.5, "pv_estimate10": 0.5}]
    assert solcast_points(integration, TZ)[0][3] == 0.5


# --- time series -----------------------------------------------------------


def test_hourly_price_fills_four_slots_and_power_integrates():
    start = datetime(2026, 10, 30, 10, tzinfo=UTC)
    slots = slot_grid(start, 8)
    values = points_to_slots([(start, None, 0.2), (start + timedelta(hours=1), None, 0.3)], slots)
    assert list(values) == [0.2] * 4 + [0.3] * 4
    energy, covered = integrate_to_slots([(start, start + timedelta(minutes=30), 2.0)], slots)
    assert energy[:2].tolist() == [0.5, 0.5] and energy[2] == 0 and covered[0] == 0.25


# --- optimiser -------------------------------------------------------------


def test_charges_cheap_and_covers_expensive():
    n = 16
    imp = [0.10] * 8 + [0.40] * 8
    plan = optimise(plan_input(n, load=0.5, imp=imp, soc=2.0), BAT, GRID)
    assert MODE_CHARGE in plan.mode[:8] and MODE_CHARGE not in plan.mode[8:]
    assert plan.battery_kwh[:8].sum() == pytest.approx(8 * 0.5 / 0.95, abs=BAT.step_kwh + 0.05)
    # Expensive half is covered from the battery.
    assert plan.grid_kwh[8:].max() < 1e-6
    assert plan.total_cost < simulate_self_consumption(plan_input(n, load=0.5, imp=imp, soc=2.0), BAT, GRID).total_cost


def test_savings_count_energy_left_at_the_end():
    """A cheap dip just before the horizon ends: the plan fills up there, plain
    self consumption ends near the reserve. That must not show as a loss."""
    bat = BatteryConfig(capacity_kwh=20, reserve_kwh=2, max_charge_kw=4, max_discharge_kw=4, cycle_cost=0.01)
    imp = [0.40] * 12 + [0.30] * 4
    inp = plan_input(16, load=0.5, imp=imp, exp=0.05, soc=10.0, terminal_value=0.42)
    plan = optimise(inp, bat, GRID)
    comparison = compare_plan(plan, inp, bat, GRID)
    assert plan.soc_kwh[-1] > simulate_self_consumption(inp, bat, GRID).soc_kwh[-1] + 2
    assert comparison["baseline_cost"] - plan.total_cost < 0  # what the old cash-only figure showed
    assert comparison["savings"] > 0
    assert comparison["no_battery_cost"] == pytest.approx(12 * 0.5 * 0.40 + 4 * 0.5 * 0.30)
    assert comparison["stored_value_change"] == pytest.approx(0.42 * (plan.soc_kwh[-1] - 10.0))


def test_battery_savings_versus_no_battery():
    """Midday PV surplus earns little; the battery moves it to the expensive evening."""
    pv = [1.0] * 8 + [0.0] * 8
    imp = [0.20] * 8 + [0.45] * 8
    inp = plan_input(16, load=0.4, pv=pv, imp=imp, exp=0.05, soc=2.0)
    plan = optimise(inp, BAT, GRID)
    comparison = compare_plan(plan, inp, BAT, GRID)
    no_battery = 8 * 0.6 * -0.05 + 8 * 0.4 * 0.45
    assert simulate_no_battery(inp, GRID, BAT) == pytest.approx(no_battery)
    assert comparison["battery_savings"] > 1.0
    assert comparison["battery_savings"] >= comparison["savings"] - 1e-6


def test_self_consumption_follows_small_load_exactly():
    plan = optimise(plan_input(96, load=0.07, imp=0.3, soc=15.0, terminal_value=0.25), BAT, GRID)
    assert np.abs(plan.grid_kwh).max() < 1e-9
    assert plan.soc_kwh[-1] == pytest.approx(15.0 - 96 * 0.07 / 0.95)


def test_reserve_is_respected():
    plan = optimise(plan_input(96, load=0.5, imp=0.3, soc=5.0), BAT, GRID)
    assert plan.soc_kwh.min() >= BAT.reserve_kwh - 1e-6


def test_forced_charge_and_hold():
    n = 8
    force = [MODE_CHARGE, MODE_CHARGE] + [None] * 4 + [MODE_HOLD, MODE_HOLD]
    inp = plan_input(n, load=0.2, imp=0.3, soc=10.0, force_mode=force, force_power_kw=np.full(n, 2.0))
    plan = optimise(inp, BAT, GRID)
    assert plan.battery_kwh[0] == pytest.approx(0.5) and plan.mode[0] == MODE_CHARGE
    assert plan.battery_kwh[6] == 0 and plan.mode[6] == MODE_HOLD


def test_grid_charge_disallowed_and_export_threshold():
    n = 16
    imp = [0.05] * 8 + [0.40] * 8
    inp = plan_input(n, load=0.2, imp=imp, soc=3.0, allow_grid_charge=np.zeros(n, bool))
    plan = optimise(inp, BAT, GRID)
    assert MODE_CHARGE not in plan.mode
    # High export price triggers battery export.
    plan = optimise(plan_input(8, load=0.1, imp=0.3, exp=[0.9] * 8, soc=18.0), BAT, GRID)
    assert MODE_EXPORT in plan.mode


def test_negative_price_curtails_and_charges():
    n = 8
    inp = plan_input(n, load=0.1, pv=1.5, imp=-0.05, exp=-0.10, soc=19.0)
    plan = optimise(inp, BAT, GRID)
    assert plan.curtail.any()
    assert plan.cost.sum() <= 0.0 + 1e-9


def test_week_plan_fills_on_cheap_weekend():
    from scenario import TARIFF, build_week

    start = datetime(2026, 10, 29, 23, 0, tzinfo=UTC)  # Friday 00:00 local
    slots, raw, pv, load = build_week(start, days=7)
    bat = BatteryConfig(150, reserve_kwh=15, max_charge_kw=10, max_discharge_kw=10, cycle_cost=0.01)
    inp = PlanInput(np.full(672, 0.25), load, pv, TARIFF.import_price(raw), TARIFF.export_price(raw), 40.0, 0.2)
    plan = optimise(inp, bat, GRID)
    weekend = slice(96, 3 * 96)
    weekdays = slice(3 * 96, 7 * 96)
    charged_weekend = plan.battery_kwh[weekend][plan.battery_kwh[weekend] > 0].sum()
    grid_weekdays = np.maximum(plan.grid_kwh[weekdays], 0).sum()
    assert charged_weekend > 60
    assert grid_weekdays < 10
    assert "marginal_cost" in plan.extra and len(plan.extra["marginal_cost"]) == 672


# --- load forecast ---------------------------------------------------------


def test_load_forecast_learns_weekday_rhythm():
    from scenario import load_history

    now = datetime(2026, 10, 30, 12, tzinfo=UTC)  # Friday
    history = load_history(now.replace(hour=0) - timedelta(hours=1), days=28)
    fc = LoadForecaster(history, TZ, default_daily_kwh=15)
    slots = slot_grid(now, 7 * 96)
    pred = fc.forecast(slots, now)
    per_day = {}
    for s, v in zip(slots, pred):
        per_day.setdefault(s.astimezone(TZ).strftime("%a"), []).append(v)
    wed = sum(per_day["Wed"])
    tue = sum(per_day["Tue"])
    assert wed > tue * 1.15
    assert 12 < tue < 18


def test_load_forecast_default_and_outlier():
    now = datetime(2026, 10, 30, 12, tzinfo=UTC)
    fc = LoadForecaster({}, TZ, default_daily_kwh=15)
    slots = slot_grid(now.replace(hour=23), 96)
    assert sum(fc.forecast(slots, now)) == pytest.approx(15, rel=0.05)

    base = {}
    t = now - timedelta(days=28)
    while t < now - timedelta(hours=12):
        base[int(t.timestamp())] = 0.15
        t += SLOT
    spike_day = (now - timedelta(days=7)).astimezone(TZ).date()
    for ts in list(base):
        if datetime.fromtimestamp(ts, TZ).date() == spike_day:
            base[ts] = 1.5  # party: 10x load for one day
    pred = LoadForecaster(base, TZ).forecast(slot_grid(now + timedelta(days=6, hours=12), 96), now)
    assert pred.mean() < 0.3


# --- prices ----------------------------------------------------------------


def test_price_history_forecast_keeps_weekend_dip():
    rng = np.random.default_rng(0)
    now = datetime(2026, 10, 30, 12, tzinfo=UTC)
    history = {}
    t = now - timedelta(days=28)
    while t < now:
        weekend = t.astimezone(TZ).weekday() >= 5
        history[int(t.timestamp())] = (0.05 if weekend else 0.12) + rng.normal(0, 0.005)
        t += SLOT
    slots = slot_grid(now, 7 * 96)
    fc = forecast_from_history(history, slots, TZ, now)
    weekend = [v for s, v in zip(slots, fc) if s.astimezone(TZ).weekday() >= 5]
    weekday = [v for s, v in zip(slots, fc) if s.astimezone(TZ).weekday() < 5]
    assert np.mean(weekend) < np.mean(weekday) - 0.05


def test_calibration_and_tariff():
    raw = np.linspace(0.0, 0.3, 64)
    tariff = Tariff(vat=0.21, energy_tax=0.09, import_markup=0.02)
    all_in = tariff.import_price(raw) + 0.01  # source with a small bias
    a, b = fit_calibration(all_in, raw)
    assert a * all_in[10] + b == pytest.approx(raw[10], abs=1e-6)
    assert tariff.to_raw(tariff.import_price(np.array([0.1])))[0] == pytest.approx(0.1)


# --- EV ----------------------------------------------------------------------


def test_ev_smart_solar_and_max():
    n = 32
    dur = np.full(n, 0.25)
    imp = np.array([0.3] * 8 + [0.1] * 8 + [0.3] * 16)
    exp = np.full(n, 0.05)
    surplus = np.zeros(n)
    req = EvRequest(EV_SMART, energy_kwh=10, deadline_index=24, min_kw=4, max_kw=10)
    plan = plan_ev(req, dur, surplus, imp, exp)
    assert plan.sum() == pytest.approx(10) and plan[8:16].sum() == pytest.approx(10)

    surplus[20:24] = 1.5
    solar = plan_ev(EvRequest(EV_SOLAR, 20, 0, min_kw=4, max_kw=10), dur, surplus, imp, exp)
    assert set(np.flatnonzero(solar)) == {20, 21, 22, 23}

    fast = plan_ev(EvRequest(EV_MAX, 5, 0, max_kw=10), dur, surplus, imp, exp)
    assert fast[:2].sum() == pytest.approx(5)


# --- manual slots / heavy loads -----------------------------------------------


def test_recurring_slot_over_midnight_and_heavy_loads():
    start = datetime(2026, 10, 30, 21, 0, tzinfo=TZ)  # Friday 21:00
    slots = slot_grid(start, 48)
    slot = ManualSlot(mode="charge", weekdays=[4], start_time=time(23, 0), end_time=time(1, 0))
    mask = slot.mask(slots, TZ)
    assert mask.sum() == 8 and mask[8] and not mask[16]

    load = HeavyLoad(name="sauna", power_kw=6, duration_min=60, start=start + timedelta(hours=1))
    assert load.profile(slots, TZ).sum() == pytest.approx(6)

    flex = HeavyLoad(name="wash", power_kw=2, duration_min=60, earliest=start, latest=start + timedelta(hours=12))
    cost = np.full(48, 0.3)
    cost[20:24] = 0.05
    chosen = flex.choose_start(slots, TZ, cost, start)
    assert chosen == slots[20]

    restored = ManualSlot.from_dict(slot.to_dict(), TZ)
    assert restored.start_time == time(23, 0) and restored.weekdays == [4]


def test_parse_octopus_style_rates_and_cents():
    from home_ems.price_forecast import unit_factor

    rates = {
        "rates": [
            {"valid_from": "2026-10-30T10:00:00+01:00", "valid_to": "2026-10-30T10:15:00+01:00", "value_inc_vat": 24.5, "value_exc_vat": 20.2},
            {"valid_from": "2026-10-30T10:15:00+01:00", "valid_to": "2026-10-30T10:30:00+01:00", "value_inc_vat": 26.0, "value_exc_vat": 21.5},
        ]
    }
    pts = extract_series(rates, TZ)
    assert [p[2] for p in pts] == [24.5, 26.0]
    assert pts[0][1] - pts[0][0] == timedelta(minutes=15)
    # All-in prices above 2 are cents, raw prices above 2 are EUR/MWh.
    assert unit_factor("auto", [24.5, 26.0], None, all_in=True) == 0.01
    assert unit_factor("auto", [95.0, 110.0], None, all_in=False) == 0.001
    assert unit_factor("auto", [0.245], None, all_in=True) == 1.0


def test_parse_zonneplan_native_and_energy_analyser_formats():
    zonneplan = {
        "forecast": [
            {"start_date": "2026-10-30T09:00:00.000000Z", "electricity_price": 2357600, "tariff_group": "normal",
             "price_tax_included": {"amount": 2357600, "currency": "EUR"}},
            {"start_date": "2026-10-30T09:15:00.000000Z", "electricity_price": 2401000, "tariff_group": "normal",
             "price_tax_included": {"amount": 2401000, "currency": "EUR"}},
        ]
    }
    pts = extract_series(zonneplan, TZ, time_key="start_date", value_key="price_tax_included.amount")
    assert [p[2] for p in pts] == [2357600.0, 2401000.0]
    assert pts[0][0] == datetime(2026, 10, 30, 9, tzinfo=UTC)
    # The last quarter keeps the 15 minute spacing (it must not fill a whole hour).
    slots = slot_grid(datetime(2026, 10, 30, 9, tzinfo=UTC), 8)
    values = points_to_slots(pts, slots)
    assert np.isfinite(values[:2]).all() and not np.isfinite(values[2:]).any()

    # Script-written forecast: naive local times, hourly, EUR/kWh.
    analyser = {"daily_forecast_data": [
        {"start": "2026-11-02T00:00:00", "end": "2026-11-02T01:00:00", "value": 0.2431},
        {"start": "2026-11-02T01:00:00", "end": "2026-11-02T02:00:00", "value": 0.2398},
    ]}
    pts = extract_series(analyser, TZ)
    assert pts[0][0] == datetime(2026, 11, 2, 0, tzinfo=TZ) and pts[1][2] == 0.2398


def test_meter_reset_spike_is_ignored():
    """An energy sensor that drops to 0 books its whole total (~13,000 kWh) in one slot."""
    now = datetime(2026, 10, 30, 12, tzinfo=UTC)  # Friday
    history = {}
    t = now - timedelta(days=10)
    while t < now - timedelta(hours=12):
        history[int(t.timestamp())] = 0.16
        t += SLOT
    spike = int(datetime(2026, 10, 29, 9, 0, tzinfo=TZ).timestamp())  # Thursday morning
    history[spike] = 13237.0
    slots = slot_grid(datetime(2026, 11, 5, 0, 0, tzinfo=TZ), 96)  # next Thursday

    unfiltered = LoadForecaster(history, TZ).forecast(slots, now)
    assert unfiltered.sum() > 1000  # the bug: the spike dominates the same-weekday estimate

    filtered = LoadForecaster(history, TZ, max_slot_kwh=30 * 0.25).forecast(slots, now)
    assert 12 < filtered.sum() < 18


def test_house_discharge_above_export_cap():
    # 20 kW of house load (EV + cooking): self consumption may deliver it all,
    # but a forced export setpoint never exceeds max_export_kw.
    bat = BatteryConfig(144, reserve_kwh=10, max_charge_kw=15, max_discharge_kw=27, max_export_kw=15, cycle_cost=0.0)
    grid = GridConfig(import_limit_kw=17, export_limit_kw=15)
    plan = optimise(plan_input(8, load=5.0, imp=0.4, exp=0.0, soc=100.0), bat, grid)
    assert np.allclose(plan.grid_kwh, 0.0, atol=1e-6)
    export = optimise(plan_input(8, load=0.0, imp=0.1, exp=0.5, soc=100.0), bat, grid)
    assert export.mode[0] == "export" and export.power_kw.max() <= 15 + 1e-6


def _skill_run(skill, days, swing_real, rng, lead_hours=48):
    """Simulate hourly updates: a forecast with a 10 ct swing whose real swing is ``swing_real``."""
    start = int(datetime(2026, 10, 1, tzinfo=UTC).timestamp())
    actual = {}
    for h in range(days * 24):
        now = start + h * 3600
        slot_ts = np.array([now + k * 3600 for k in range(lead_hours + 1)])
        shape = np.sin(slot_ts / 86400 * 2 * np.pi)
        raw = 0.10 + 0.10 * shape
        firm = slot_ts < now + 12 * 3600
        for ts, s in zip(slot_ts, shape):
            if ts < now + 12 * 3600:
                actual.setdefault(int(ts), 0.10 + swing_real * s + rng.normal(0, 0.01))
        origin = np.where(firm, "day_ahead", "fc").astype(object)
        skill.observe(now, slot_ts, raw, firm, origin, actual)
    return skill


def test_forecast_skill_prior_trusts_far_ahead_less():
    skill = ForecastSkill()
    assert skill.skill("fc", 1).trust > skill.skill("fc", 5).trust >= 0.4
    assert skill.skill("history", 5).trust == 1.0
    assert skill.skill("fc", 5).error > skill.skill("fc", 1).error


def test_forecast_skill_learns_damping_and_keeps_firm_prices():
    rng = np.random.default_rng(1)
    skill = _skill_run(ForecastSkill(), days=30, swing_real=0.05, rng=rng)
    k = skill.skill("fc", 1)
    assert 0.4 < k.trust < 0.65 and k.samples > 100
    assert k.error < 0.02 and abs(k.bias) < 0.01

    now = int(datetime(2026, 11, 1, tzinfo=UTC).timestamp())
    slot_ts = np.array([now + k * 3600 for k in range(48)])
    raw = 0.10 + 0.10 * np.sin(slot_ts / 86400 * 2 * np.pi)
    firm = slot_ts < now + 12 * 3600
    origin = np.where(firm, "day_ahead", "fc").astype(object)
    out, margin = skill.adjust(now, slot_ts, raw, firm, origin)
    assert np.array_equal(out[firm], raw[firm]) and not margin[firm].any()
    assert np.ptp(out[~firm]) < 0.7 * np.ptp(raw[~firm]) and (margin[~firm] > 0).all()

    restored = ForecastSkill.from_dict(skill.to_dict())
    assert abs(restored.skill("fc", 1).trust - k.trust) < 1e-3
    assert restored.pending.keys() == skill.pending.keys()


def test_forecast_skill_keeps_a_good_forecast():
    rng = np.random.default_rng(2)
    skill = _skill_run(ForecastSkill(), days=30, swing_real=0.10, rng=rng)
    assert skill.skill("fc", 1).trust > 0.9


def test_hold_level_is_kept_unless_the_price_is_high():
    n = 96
    imp = np.full(n, 0.30)
    imp[60:68] = 0.80  # two hour evening peak

    def soc(hold_cost):
        bat = BatteryConfig(20, reserve_kwh=2, max_charge_kw=4, max_discharge_kw=4, cycle_cost=0.01,
                            hold_kwh=10, hold_cost=hold_cost)
        return optimise(plan_input(n, imp=imp, soc=14.0, terminal_value=0.10), bat, GRID).soc_kwh

    free, held = soc(0.0), soc(0.10)
    assert free[60] < 6  # without a hold level the battery is used at normal prices
    assert held[60] > 10  # with one it is kept for the peak...
    assert held[68] < 10  # ...and allowed to dip below it during the peak


def test_trading_aggressiveness_scales_from_self_consumption_to_full_trading():
    n = 192
    hour = (np.arange(n) // 4) % 24
    imp = np.where(hour < 6, 0.30, np.where((hour >= 17) & (hour < 21), 0.45, 0.38))
    imp[hour == 18] = 0.60
    pv = np.where((hour >= 10) & (hour < 15), 0.6, 0.0)
    bat = BatteryConfig(40, reserve_kwh=4, max_charge_kw=8, max_discharge_kw=8, cycle_cost=0.01)

    def plan(pct):
        inp = plan_input(n, load=0.3, pv=pv, imp=imp, exp=imp, soc=20.0, terminal_value=0.30,
                         trade_hurdle=trade_hurdle(pct))
        return inp, optimise(inp, bat, GRID)

    moved = {pct: float(np.abs(plan(pct)[1].battery_kwh).sum()) for pct in (100, 75, 50, 0)}
    assert moved[100] > moved[75] > moved[50] >= moved[0]
    inp, conservative = plan(0)
    assert set(conservative.mode) == {"self_consumption"}
    assert np.allclose(conservative.soc_kwh, simulate_self_consumption(inp, bat, GRID).soc_kwh, atol=0.3)
    assert trade_hurdle(100) == 0.0 and trade_hurdle(50) == pytest.approx(0.10)
