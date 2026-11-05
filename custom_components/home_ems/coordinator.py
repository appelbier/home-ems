"""Coordinator: gathers forecasts, runs the planner and drives the inverter."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
import logging
import math
from typing import Any

import numpy as np

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.const import UnitOfEnergy
from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_change
from homeassistant.helpers.script import Script, async_validate_actions_config
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ALLOW_CURTAIL,
    CONF_BATTERY,
    CONF_CAPACITY,
    CONF_CHARGE_EFF,
    CONF_CONNECTED_ENTITY,
    CONF_CONTROL,
    CONF_CYCLE_COST,
    CONF_DEFAULT_DAILY_KWH,
    CONF_DISCHARGE_EFF,
    CONF_DURATION_MIN,
    CONF_EARLIEST,
    CONF_ENABLED,
    CONF_END,
    CONF_END_TIME,
    CONF_ENERGY_ENTITY,
    CONF_ENERGY_TAX,
    CONF_EV,
    CONF_EV_BATTERY_KWH,
    CONF_EV_CONNECTED_STATES,
    CONF_EV_EFFICIENCY,
    CONF_EV_MAX_KW,
    CONF_EV_MIN_KW,
    CONF_EV_MODE,
    CONF_EV_PHASES,
    CONF_EV_READY_BY,
    CONF_EV_SESSION_ENERGY,
    CONF_EV_SOC_ENTITY,
    CONF_EV_START,
    CONF_EV_STOP,
    CONF_EV_TARGET_SOC,
    CONF_EV_VOLTAGE,
    CONF_EXPORT_FEE,
    CONF_EXPORT_LIMIT,
    CONF_EXPORT_MODE,
    CONF_EXPORT_VAT,
    CONF_FORECAST_MARGIN,
    CONF_FORECASTS,
    CONF_HISTORY_SOURCE,
    CONF_HOLD_COST,
    CONF_HOLD_PCT,
    CONF_NET_METERING_UNTIL,
    CONF_GRID,
    CONF_HEAVY_LOADS,
    CONF_HISTORY_DAYS,
    CONF_HISTORY_WEEKS,
    CONF_IMPORT_LIMIT,
    CONF_IMPORT_MARKUP,
    CONF_LATEST,
    CONF_LOAD,
    CONF_MANUAL_SLOTS,
    CONF_MAX_CHARGE_KW,
    CONF_MAX_DISCHARGE_KW,
    CONF_MAX_EXPORT_KW,
    CONF_MAX_LOAD_KW,
    CONF_MAX_SOC_PCT,
    CONF_MODE,
    CONF_NAME,
    CONF_PLAN_DAYS,
    CONF_POWER_KW,
    CONF_PRICES,
    CONF_PV_SCALE,
    CONF_REAPPLY_MINUTES,
    CONF_RESERVE_PCT,
    CONF_SOC_ENTITY,
    CONF_SOC_STEP,
    CONF_SOC_UNIT,
    CONF_SOLAR,
    CONF_SOURCE,
    CONF_START,
    CONF_START_TIME,
    CONF_SUBTRACT_ENTITIES,
    CONF_TERMINAL_VALUE,
    CONF_UPDATE_INTERVAL,
    CONF_VAT_PCT,
    CONF_WEEKDAYS,
    DOMAIN,
    SET_AGGRESSIVENESS,
    SET_ALLOW_BATTERY_EXPORT,
    SET_ALLOW_GRID_CHARGE,
    SET_CONTROL_ENABLED,
    SET_CYCLE_COST,
    SET_EV_ALLOW_BATTERY,
    SET_EV_ENERGY_KWH,
    SET_EV_MODE,
    SET_EV_READY_BY,
    SET_EV_TARGET_SOC,
    SET_FORECAST_MARGIN,
    SET_FORECAST_TRUST,
    SET_HOLD_COST,
    SET_HOLD_PCT,
    SET_LOAD_MARGIN_PCT,
    SET_MAX_CHARGE_KW,
    SET_MAX_CHARGE_PRICE,
    SET_MAX_DISCHARGE_KW,
    SET_MAX_SOC_PCT,
    SET_MIN_EXPORT_PRICE,
    SET_PV_PESSIMISM,
    SET_RESERVE_PCT,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .ev_planner import EV_OFF, EV_SMART, EvRequest, plan_ev
from .load_forecast import LoadForecaster, plausible_load
from .models import HeavyLoad, ManualSlot
from .optimizer import (
    MODE_CHARGE,
    MODE_EXPORT,
    MODE_HOLD,
    MODE_SELF_CONSUMPTION,
    MODES,
    BatteryConfig,
    GridConfig,
    PlanInput,
    PlanResult,
    compare_plan,
    optimise,
    trade_hurdle,
)
from .forecast_skill import ForecastSkill
from .price_forecast import Tariff, fit_calibration, forecast_from_history, terminal_value
from .sources import PriceSource, SolarSource
from .timeseries import (
    SLOT,
    SLOT_HOURS,
    SLOT_SECONDS,
    expand_points,
    fill_by_time_of_day,
    floor_slot,
    integrate_to_slots,
    points_to_slots,
    slot_grid,
)

_LOGGER = logging.getLogger(__name__)

LOAD_SYNC_INTERVAL = timedelta(hours=1)
FLAT_FORECAST_STD = 0.002  # EUR/kWh; real day profiles vary far more than this
FLEX_LOCK = timedelta(hours=1)  # a flexible load's start is fixed this long before it
IDLE_POWER_KW = 0.1  # charge/export setpoints below this are sent as hold


@dataclass
class EmsData:
    """Everything the entities show, rebuilt every planning cycle."""

    updated: datetime
    current: dict[str, Any]
    slots: list[dict[str, Any]]
    days: list[dict[str, Any]]
    totals: dict[str, Any]
    ev: dict[str, Any]
    prices: dict[str, Any]
    pv: dict[str, Any]
    load: dict[str, Any]
    heavy_loads: list[dict[str, Any]]
    manual_slots: list[dict[str, Any]]
    warnings: list[str] = field(default_factory=list)


@dataclass
class _Problem:
    """Inputs handed to the executor job (no Home Assistant objects)."""

    slot_starts: list[datetime]
    now: datetime
    tz: Any
    inp: PlanInput
    bat: BatteryConfig
    grid: GridConfig
    flexible: list[HeavyLoad]
    ev: EvRequest | None
    ev_allow_battery: bool


def _stop_soc(mode: str, target: float, soc: float, reserve: float, max_soc: float) -> int:
    """Whole-percent target SoC for the inverter, rounded in the direction of travel.

    Inverters (and the Deye ESPHome firmware) report whole percents and stop a
    charge once ``soc >= target`` (a discharge once ``soc <= target``). Plain
    rounding often gives the current SoC for a short or slow block, so the
    command would end before it started. Charge targets round up and lie at
    least 1 % above the SoC, discharge targets the other way, within the limits.
    """
    if mode == MODE_CHARGE:
        return int(min(max(math.ceil(target - 1e-6), math.floor(soc) + 1), math.ceil(max_soc)))
    if mode == MODE_EXPORT:
        return int(max(min(math.floor(target + 1e-6), math.ceil(soc) - 1), math.ceil(reserve)))
    return int(round(target))


def _solve(problem: _Problem) -> tuple[PlanResult, np.ndarray, dict[str, datetime | None]]:
    """Two-pass plan: base plan -> place EV and flexible loads -> final plan."""
    inp = problem.inp
    plan = optimise(inp, problem.bat, problem.grid)
    ev_kwh = np.zeros(inp.slots)
    chosen: dict[str, datetime | None] = {}
    if problem.ev is None and not problem.flexible:
        plan.extra["comparison"] = compare_plan(plan, inp, problem.bat, problem.grid)
        return plan, ev_kwh, chosen

    marginal = plan.extra["marginal_cost"]
    extra_load = np.zeros(inp.slots)
    for load in problem.flexible:
        start = load.choose_start(problem.slot_starts, problem.tz, marginal, problem.now)
        chosen[load.id] = start
        if start is not None:
            extra_load += load.profile(problem.slot_starts, problem.tz, start)

    if problem.ev is not None:
        surplus = np.maximum(inp.pv_kwh - inp.load_kwh - extra_load, 0.0)
        grid_price = marginal if problem.ev_allow_battery else inp.import_price
        ev_kwh = plan_ev(problem.ev, inp.duration_h, surplus, grid_price, inp.export_price)

    final = PlanInput(
        duration_h=inp.duration_h,
        load_kwh=inp.load_kwh + extra_load + ev_kwh,
        pv_kwh=inp.pv_kwh,
        import_price=inp.import_price,
        export_price=inp.export_price,
        initial_soc_kwh=inp.initial_soc_kwh,
        terminal_value=inp.terminal_value,
        allow_grid_charge=inp.allow_grid_charge,
        allow_battery_export=inp.allow_battery_export,
        protected_load_kwh=np.zeros(inp.slots) if problem.ev_allow_battery else ev_kwh,
        force_mode=inp.force_mode,
        force_power_kw=inp.force_power_kw,
        trade_hurdle=inp.trade_hurdle,
    )
    plan = optimise(final, problem.bat, problem.grid)
    plan.extra["comparison"] = compare_plan(plan, final, problem.bat, problem.grid)
    plan.extra["load_kwh"] = final.load_kwh
    return plan, ev_kwh, chosen


class EmsCoordinator(DataUpdateCoordinator[EmsData]):
    """Plans the battery for the coming week and executes the current slot."""

    def __init__(self, hass: HomeAssistant, conf: dict) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=None,  # set up from configuration.yaml
            name=DOMAIN,
            update_interval=timedelta(minutes=conf[CONF_UPDATE_INTERVAL]),
        )
        self.conf = conf
        self.tz = dt_util.get_default_time_zone()
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        prices = conf[CONF_PRICES]
        self.tariff = Tariff(
            vat=prices[CONF_VAT_PCT] / 100,
            energy_tax=prices[CONF_ENERGY_TAX],
            import_markup=prices[CONF_IMPORT_MARKUP],
            export_mode=prices[CONF_EXPORT_MODE],
            export_fee=prices[CONF_EXPORT_FEE],
            export_vat=prices[CONF_EXPORT_VAT],
        )
        self.price_source = PriceSource(hass, prices[CONF_SOURCE], self.tariff, self.tz)
        self.forecast_sources = [PriceSource(hass, c, self.tariff, self.tz) for c in prices[CONF_FORECASTS]]
        self.history_source = (
            PriceSource(hass, prices[CONF_HISTORY_SOURCE], self.tariff, self.tz)
            if CONF_HISTORY_SOURCE in prices
            else None
        )
        self.solar = SolarSource(hass, conf[CONF_SOLAR], self.tz)
        self.ev_conf: dict | None = conf.get(CONF_EV)
        self.settings: dict[str, Any] = self._default_settings()
        self.manual_slots: list[ManualSlot] = []
        self.heavy_loads: list[HeavyLoad] = []
        self.load_history: dict[int, float] = {}
        self.price_history: dict[int, float] = {}
        self.price_skill = ForecastSkill()
        self._scripts: dict[str, Script] = {}
        self._ev_scripts: dict[str, Script] = {}
        self._last_control: tuple | None = None
        self._last_control_at: datetime | None = None
        self._last_ev: tuple | None = None
        self._last_ev_at: datetime | None = None
        self._last_load_sync: datetime | None = None
        self._last_soc_kwh: float | None = None
        self.rejected_load: dict[int, float] = {}  # slot -> implausible kWh reading
        self.load_excluded: list[tuple[int, int]] = []  # [start, end) UTC timestamps kept out of the history
        self._history_days = 0
        self._unsub: list = []

    # ------------------------------------------------------------------ setup

    def _default_settings(self) -> dict[str, Any]:
        bat = self.conf[CONF_BATTERY]
        ev = self.ev_conf or {}
        return {
            SET_CONTROL_ENABLED: self.conf[CONF_CONTROL].get(CONF_ENABLED, False),
            SET_ALLOW_GRID_CHARGE: True,
            SET_ALLOW_BATTERY_EXPORT: True,
            SET_EV_ALLOW_BATTERY: False,
            SET_MAX_CHARGE_KW: bat[CONF_MAX_CHARGE_KW],
            SET_MAX_DISCHARGE_KW: bat[CONF_MAX_DISCHARGE_KW],
            SET_RESERVE_PCT: bat[CONF_RESERVE_PCT],
            SET_HOLD_PCT: bat.get(CONF_HOLD_PCT, bat[CONF_RESERVE_PCT]),
            SET_HOLD_COST: bat[CONF_HOLD_COST],
            SET_MAX_SOC_PCT: bat[CONF_MAX_SOC_PCT],
            SET_MAX_CHARGE_PRICE: 1.0,
            SET_MIN_EXPORT_PRICE: 0.0,
            SET_CYCLE_COST: bat[CONF_CYCLE_COST],
            SET_AGGRESSIVENESS: 100.0,
            SET_PV_PESSIMISM: 0.3,
            SET_LOAD_MARGIN_PCT: 5.0,
            SET_FORECAST_MARGIN: self.conf[CONF_PRICES][CONF_FORECAST_MARGIN],
            SET_FORECAST_TRUST: True,
            SET_EV_MODE: ev.get(CONF_EV_MODE, EV_SMART),
            SET_EV_TARGET_SOC: ev.get(CONF_EV_TARGET_SOC, 80.0),
            SET_EV_ENERGY_KWH: 0.0,
            SET_EV_READY_BY: (ev.get(CONF_EV_READY_BY) or time(7, 0)).strftime("%H:%M"),
        }

    async def async_setup(self) -> None:
        stored = await self._store.async_load() or {}
        # A setting changed from the dashboard survives restarts, unless its
        # YAML value changed since: then the new YAML value wins.
        defaults = stored.get("settings_defaults") or {}
        for key, value in (stored.get("settings") or {}).items():
            if key in self.settings and (key not in defaults or defaults[key] == self.settings[key]):
                self.settings[key] = value
        self.load_history = {int(k): float(v) for k, v in (stored.get("load_history") or {}).items()}
        self.load_excluded = [(int(a), int(b)) for a, b in stored.get("load_excluded") or []]
        for ts in [ts for ts in self.load_history if self._load_excluded(ts)]:
            del self.load_history[ts]
        # Drop glitches stored before they were filtered (e.g. a meter reset spike).
        for ts, value in list(self.load_history.items()):
            if not plausible_load(value, self._max_load_slot_kwh):
                del self.load_history[ts]
                self.rejected_load[ts] = value
        self.price_history = {int(k): float(v) for k, v in (stored.get("price_history") or {}).items()}
        self.price_skill = ForecastSkill.from_dict(stored.get("forecast_skill"))
        self.solar.restore(stored.get("solar_cache"))
        for source in self.forecast_sources:
            source.restore((stored.get("forecast_cache") or {}).get(source.name))

        for i, item in enumerate(self.conf[CONF_MANUAL_SLOTS]):
            self.manual_slots.append(self._slot_from_conf(item, f"yaml_slot_{i}", "yaml"))
        for item in stored.get("manual_slots") or []:
            self.manual_slots.append(ManualSlot.from_dict(item, self.tz))
        for i, item in enumerate(self.conf[CONF_HEAVY_LOADS]):
            self.heavy_loads.append(self._load_from_conf(item, f"yaml_load_{i}", "yaml"))
        for item in stored.get("heavy_loads") or []:
            self.heavy_loads.append(HeavyLoad.from_dict(item, self.tz))

        control = self.conf[CONF_CONTROL]
        for mode in MODES:
            if mode in control:
                actions = await async_validate_actions_config(self.hass, control[mode])
                self._scripts[mode] = Script(
                    self.hass, actions, f"Home EMS {mode}", DOMAIN, script_mode="restart", logger=_LOGGER
                )
        if self.ev_conf:
            for key in (CONF_EV_START, CONF_EV_STOP):
                if key in self.ev_conf:
                    actions = await async_validate_actions_config(self.hass, self.ev_conf[key])
                    self._ev_scripts[key] = Script(
                        self.hass, actions, f"Home EMS EV {key}", DOMAIN, script_mode="restart", logger=_LOGGER
                    )

        self._unsub.append(
            async_track_time_change(self.hass, self._on_slot_boundary, minute=[0, 15, 30, 45], second=5)
        )

    async def async_bootstrap_history(self) -> None:
        """Load history from the recorder (run once HA has started)."""
        try:
            await self._sync_load_history(self.conf[CONF_LOAD][CONF_HISTORY_DAYS])
        except Exception as err:  # noqa: BLE001 - planning works with the default profile
            _LOGGER.warning("Could not read load history from the recorder: %s", err)
        try:
            await self._bootstrap_price_history()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not read price history from the recorder: %s", err)

    @callback
    @callback
    def async_refresh_when_soc_available(self) -> None:
        """Plan as soon as the SoC sensor reports a number.

        Its integration (a BMS over Bluetooth, MQTT, Modbus...) can come up
        after Home Assistant has started; don't wait a whole update interval.
        """
        entity = self.conf[CONF_BATTERY][CONF_SOC_ENTITY]

        @callback
        def _changed(event) -> None:
            new = event.data.get("new_state")
            try:
                float(new.state)
            except (AttributeError, TypeError, ValueError):
                return
            unsub()
            if unsub in self._unsub:
                self._unsub.remove(unsub)
            self.hass.async_create_task(self.async_request_refresh())

        unsub = async_track_state_change_event(self.hass, [entity], _changed)
        self._unsub.append(unsub)

    def async_shutdown_listeners(self) -> None:
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()

    @callback
    def _on_slot_boundary(self, _now: datetime) -> None:
        self.hass.async_create_task(self.async_request_refresh())

    def _slot_from_conf(self, item: dict, slot_id: str, source: str) -> ManualSlot:
        return ManualSlot(
            mode=item[CONF_MODE],
            power_kw=item.get(CONF_POWER_KW),
            start=self._aware(item.get(CONF_START)),
            end=self._aware(item.get(CONF_END)),
            weekdays=item.get(CONF_WEEKDAYS) or [],
            start_time=item.get(CONF_START_TIME),
            end_time=item.get(CONF_END_TIME),
            name=item.get(CONF_NAME, ""),
            id=slot_id,
            source=source,
        )

    def _load_from_conf(self, item: dict, load_id: str, source: str) -> HeavyLoad:
        return HeavyLoad(
            name=item[CONF_NAME],
            power_kw=item[CONF_POWER_KW],
            duration_min=item[CONF_DURATION_MIN],
            start=self._aware(item.get(CONF_START)),
            weekdays=item.get(CONF_WEEKDAYS) or [],
            start_time=item.get(CONF_START_TIME),
            earliest=self._aware(item.get(CONF_EARLIEST)),
            latest=self._aware(item.get(CONF_LATEST)),
            id=load_id,
            source=source,
        )

    def _aware(self, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=self.tz)
        return dt_util.as_utc(value)

    @property
    def _max_load_slot_kwh(self) -> float:
        return self.conf[CONF_LOAD][CONF_MAX_LOAD_KW] * SLOT_HOURS

    # ------------------------------------------------------------ persistence

    def _data_to_save(self) -> dict:
        return {
            "settings": self.settings,
            "settings_defaults": self._default_settings(),
            "manual_slots": [s.to_dict() for s in self.manual_slots if s.source != "yaml"],
            "heavy_loads": [h.to_dict() for h in self.heavy_loads if h.source != "yaml"],
            "load_history": {str(k): round(v, 4) for k, v in self.load_history.items()},
            "price_history": {str(k): round(v, 5) for k, v in self.price_history.items()},
            "solar_cache": self.solar.cache(),
            "load_excluded": [list(r) for r in self.load_excluded],
            "forecast_cache": {s.name: s.cache() for s in self.forecast_sources},
            "forecast_skill": self.price_skill.to_dict(),
        }

    def schedule_save(self) -> None:
        self._store.async_delay_save(self._data_to_save, 30)

    # ---------------------------------------------------- user modifications

    async def async_set_setting(self, key: str, value: Any) -> None:
        self.settings[key] = value
        self.schedule_save()
        await self.async_request_refresh()

    def add_manual_slot(self, slot: ManualSlot) -> None:
        self.manual_slots.append(slot)
        self.schedule_save()

    def remove_manual_slot(self, slot_id: str) -> bool:
        before = len(self.manual_slots)
        self.manual_slots = [s for s in self.manual_slots if s.id != slot_id]
        self.schedule_save()
        return len(self.manual_slots) < before

    def clear_manual_slots(self, include_yaml: bool = False) -> None:
        self.manual_slots = [s for s in self.manual_slots if s.source == "yaml" and not include_yaml]
        self.schedule_save()

    def add_heavy_load(self, load: HeavyLoad) -> None:
        self.heavy_loads.append(load)
        self.schedule_save()

    def remove_heavy_load(self, load_id: str) -> bool:
        before = len(self.heavy_loads)
        self.heavy_loads = [h for h in self.heavy_loads if h.id != load_id]
        self.schedule_save()
        return len(self.heavy_loads) < before

    def _load_excluded(self, ts: int) -> bool:
        return any(a <= ts < b for a, b in self.load_excluded)

    def exclude_load_history(self, start: datetime, end: datetime) -> int:
        """Keep a period (e.g. a test with simulated meter values) out of the load model.

        Removes the slots now and remembers the period, so the hourly re-sync from
        the recorder statistics does not bring them back. Returns the slots removed.
        """
        a = int(floor_slot(start).timestamp())
        b = int(end.timestamp())
        self.load_excluded.append((a, b))
        removed = [ts for ts in self.load_history if a <= ts < b]
        for ts in removed:
            del self.load_history[ts]
        self.rejected_load = {ts: v for ts, v in self.rejected_load.items() if not a <= ts < b}
        self.schedule_save()
        return len(removed)

    async def async_clear_load_exclusions(self) -> None:
        """Forget all excluded periods and re-import them from the recorder."""
        self.load_excluded = []
        await self._sync_load_history(self.conf[CONF_LOAD][CONF_HISTORY_DAYS])

    def clear_heavy_loads(self, include_yaml: bool = False) -> None:
        self.heavy_loads = [h for h in self.heavy_loads if h.source == "yaml" and not include_yaml]
        self.schedule_save()

    async def async_refresh_forecasts(self) -> None:
        now = dt_util.utcnow()
        await self.price_source.async_points(now, force=True)
        for source in self.forecast_sources:
            await source.async_points(now, force=True)
        await self.solar.async_points(now, force=True)
        self.schedule_save()
        await self.async_request_refresh()

    # -------------------------------------------------------------- history

    async def _statistics(self, start: datetime, end: datetime, ids: set[str], period: str, units, types):
        return await get_instance(self.hass).async_add_executor_job(
            statistics_during_period, self.hass, start, end, ids, period, units, types
        )

    @staticmethod
    def _row_ts(row: dict) -> int:
        start = row["start"]
        return int(start.timestamp()) if isinstance(start, datetime) else int(start)

    def _load_entity_problem(self) -> str | None:
        """Why the recorder keeps no usable statistics for the load sensor, if it can tell."""
        entity = self.conf[CONF_LOAD].get(CONF_ENERGY_ENTITY)
        if not entity:
            return "no load.energy_entity configured: the load model only learns from one"
        state = self.hass.states.get(entity)
        if state is None:
            return f"load sensor {entity} does not exist"
        state_class = state.attributes.get("state_class")
        if state_class not in ("total", "total_increasing"):
            return (
                f"load sensor {entity} has state_class '{state_class or 'none'}': set state_class "
                "total_increasing (device_class energy, unit kWh) so the recorder keeps the "
                "long-term statistics the load model reads. History builds up from then on."
            )
        return None

    async def _sync_load_history(self, days: int) -> None:
        load_conf = self.conf[CONF_LOAD]
        entity = load_conf.get(CONF_ENERGY_ENTITY)
        if not entity:
            return
        now = dt_util.utcnow()
        start = now - timedelta(days=days)
        ids = {entity, *load_conf[CONF_SUBTRACT_ENTITIES]}
        units = {"energy": UnitOfEnergy.KILO_WATT_HOUR}
        hourly = await self._statistics(start, now, ids, "hour", units, {"change"})
        short = await self._statistics(max(start, now - timedelta(days=9)), now, ids, "5minute", units, {"change"})

        def per_slot(rows_by_id: dict, period_s: int) -> dict[str, dict[int, tuple[float, int]]]:
            out: dict[str, dict[int, tuple[float, int]]] = {}
            for stat_id, rows in rows_by_id.items():
                slots: dict[int, tuple[float, int]] = {}
                for row in rows:
                    change = row.get("change")
                    if change is None:
                        continue
                    ts = self._row_ts(row)
                    if period_s >= SLOT_SECONDS:
                        parts = period_s // SLOT_SECONDS
                        for k in range(parts):
                            total, count = slots.get(ts + k * SLOT_SECONDS, (0.0, 0))
                            slots[ts + k * SLOT_SECONDS] = (total + change / parts, count + 1)
                    else:
                        key = ts - ts % SLOT_SECONDS
                        total, count = slots.get(key, (0.0, 0))
                        slots[key] = (total + change, count + 1)
                out[stat_id] = slots
            return out

        hour_slots = per_slot(hourly, 3600)
        fine_slots = per_slot(short, 300)
        current = int(floor_slot(now).timestamp())

        def value(stat_id: str, ts: int) -> float | None:
            fine = fine_slots.get(stat_id, {}).get(ts)
            if fine and fine[1] >= 3:
                return fine[0]
            coarse = hour_slots.get(stat_id, {}).get(ts)
            return coarse[0] if coarse else None

        keys = set(hour_slots.get(entity, {})) | set(fine_slots.get(entity, {}))
        for ts in keys:
            if ts >= current or self._load_excluded(ts):
                continue
            main = value(entity, ts)
            if main is None:
                continue
            for sub in load_conf[CONF_SUBTRACT_ENTITIES]:
                main -= value(sub, ts) or 0.0
            main = max(0.0, main)
            if plausible_load(main, self._max_load_slot_kwh):
                self.load_history[ts] = main
            else:
                self.load_history.pop(ts, None)
                self.rejected_load[ts] = main

        cutoff = now.timestamp() - (load_conf[CONF_HISTORY_DAYS] + 1) * 86400
        self.load_history = {k: v for k, v in self.load_history.items() if k >= cutoff}
        self.rejected_load = {k: v for k, v in self.rejected_load.items() if k >= cutoff}
        self.load_excluded = [(a, b) for a, b in self.load_excluded if b >= cutoff]
        self._last_load_sync = now
        self.schedule_save()

    async def _bootstrap_price_history(self) -> None:
        """Seed the price history from the hourly mean statistics of a price sensor.

        Uses ``prices.history_source`` when set (e.g. a sensor whose state is
        the current price), else the day-ahead source entity itself.
        """
        source = self.history_source or self.price_source
        entity = source.entity_id
        if len(self.price_history) >= 7 * 96 or not entity:
            return
        weeks = self.conf[CONF_PRICES][CONF_HISTORY_WEEKS]
        now = dt_util.utcnow()
        stats = await self._statistics(now - timedelta(weeks=weeks), now, {entity}, "hour", None, {"mean"})
        state = self.hass.states.get(entity)
        hint = state.attributes.get("unit_of_measurement") if state else None
        for row in stats.get(entity, []):
            mean = row.get("mean")
            if mean is None:
                continue
            raw = source.normalise_value(mean, hint)
            ts = self._row_ts(row)
            for k in range(4):
                self.price_history.setdefault(ts + k * SLOT_SECONDS, raw)
        if self.price_history:
            _LOGGER.info("Bootstrapped %d price slots from recorder statistics", len(self.price_history))

    # ------------------------------------------------------------- forecasts

    def _read_soc(self, warnings: list[str]) -> float:
        bat = self.conf[CONF_BATTERY]
        state = self.hass.states.get(bat[CONF_SOC_ENTITY])
        try:
            value = float(state.state)  # type: ignore[union-attr]
        except (AttributeError, TypeError, ValueError):
            if self._last_soc_kwh is not None:
                warnings.append("battery SoC unavailable, using last known value")
                return self._last_soc_kwh
            if state is None:
                raise UpdateFailed(
                    f"battery SoC entity {bat[CONF_SOC_ENTITY]} does not exist: check the entity id "
                    "(Settings > Devices & services > Entities)"
                ) from None
            raise UpdateFailed(
                f"battery SoC entity {bat[CONF_SOC_ENTITY]} is '{state.state}'; "
                "planning starts as soon as it reports a number"
            ) from None
        soc = value * bat[CONF_CAPACITY] / 100 if bat[CONF_SOC_UNIT] == "percent" else value
        self._last_soc_kwh = soc
        return soc

    async def _prices(self, slot_starts: list[datetime], now: datetime, warnings: list[str]):
        firm_points = await self.price_source.async_points(now)
        if self.price_source.error:
            warnings.append(f"price source: {self.price_source.error}")
        self.price_history.update(expand_points(firm_points))
        cutoff = now.timestamp() - (self.conf[CONF_PRICES][CONF_HISTORY_WEEKS] + 1) * 7 * 86400
        self.price_history = {k: v for k, v in self.price_history.items() if k >= cutoff}

        raw = points_to_slots(firm_points, slot_starts)
        firm = np.isfinite(raw)
        origin = np.where(firm, "day_ahead", "").astype(object)
        for source in self.forecast_sources:
            points = await source.async_points(now)
            if source.error:
                warnings.append(f"forecast {source.name}: {source.error}")
            if not points:
                continue
            overlap = expand_points(points)
            common = [ts for ts in overlap if ts in self.price_history]
            source.calibration = fit_calibration(
                np.array([overlap[ts] for ts in common]),
                np.array([self.price_history[ts] for ts in common]),
            )
            a, b = source.calibration
            values = points_to_slots(points, slot_starts) * a + b
            fill = ~np.isfinite(raw) & np.isfinite(values)
            flat_days = self._drop_flat_days(values, fill, slot_starts)
            if flat_days:
                warnings.append(
                    f"forecast {source.name}: ignored flat values for {', '.join(flat_days)} "
                    "(source has no real prediction there)"
                )
            raw[fill] = values[fill]
            origin[fill] = source.name

        missing = ~np.isfinite(raw)
        if missing.any():
            history = forecast_from_history(
                self.price_history, slot_starts, self.tz, now, self.conf[CONF_PRICES][CONF_HISTORY_WEEKS]
            )
            fill = missing & np.isfinite(history)
            raw[fill] = history[fill]
            origin[fill] = "history"
        missing = ~np.isfinite(raw)
        if missing.any():
            raw = fill_by_time_of_day(raw, slot_starts, self.tz)
            origin[missing & np.isfinite(raw)] = "repeat"
        if not np.isfinite(raw).any():
            raise UpdateFailed("no electricity prices available")
        raw[~np.isfinite(raw)] = float(np.nanmean(raw))
        return raw, firm, origin

    def _drop_flat_days(self, values: np.ndarray, fill: np.ndarray, slot_starts: list[datetime]) -> list[str]:
        """Clear ``fill`` for local days where a forecast is a flat line.

        A source that has no prediction sometimes returns one constant price
        for whole days (a placeholder). Planning on that would hide the
        weekday/weekend pattern, so those days fall back to the history model.
        """
        days: dict[str, list[int]] = {}
        for i in np.flatnonzero(fill):
            days.setdefault(slot_starts[i].astimezone(self.tz).date().isoformat(), []).append(int(i))
        flat = []
        for day, idx in days.items():
            if len(idx) >= 8 and float(np.std(values[idx])) < FLAT_FORECAST_STD:
                fill[idx] = False
                flat.append(day)
        return flat

    async def _pv(self, slot_starts: list[datetime], now: datetime, warnings: list[str]) -> np.ndarray:
        if not self.solar.configured:
            return np.zeros(len(slot_starts))
        points = await self.solar.async_points(now)
        if self.solar.error:
            warnings.append(f"solcast: {self.solar.error}")
        if self.solar.uses_api:
            self.schedule_save()
        p50, covered = integrate_to_slots([(s, e, p) for s, e, p, _, _ in points], slot_starts)
        p10, _ = integrate_to_slots([(s, e, p) for s, e, _, p, _ in points], slot_starts)
        weight = float(self.settings[SET_PV_PESSIMISM])
        pv = ((1 - weight) * p50 + weight * p10) * self.conf[CONF_SOLAR][CONF_PV_SCALE]
        pv[covered <= 0] = np.nan
        if np.isnan(pv).any():
            pv = fill_by_time_of_day(pv, slot_starts, self.tz, scale=0.8)
        return np.nan_to_num(pv, nan=0.0)

    def _ev_request(self, slot_starts: list[datetime], now: datetime) -> tuple[EvRequest | None, dict]:
        info: dict[str, Any] = {"configured": self.ev_conf is not None}
        if not self.ev_conf:
            return None, info
        mode = self.settings[SET_EV_MODE]
        info["mode"] = mode
        connected = True
        if entity := self.ev_conf.get(CONF_CONNECTED_ENTITY):
            state = self.hass.states.get(entity)
            states = {s.lower() for s in self.ev_conf[CONF_EV_CONNECTED_STATES]}
            connected = state is not None and state.state.lower() in states
        info["connected"] = connected

        target = float(self.settings[SET_EV_TARGET_SOC])
        need_kwh = None
        if entity := self.ev_conf.get(CONF_EV_SOC_ENTITY):
            state = self.hass.states.get(entity)
            try:
                soc = float(state.state)  # type: ignore[union-attr]
                info["soc"] = soc
                need_kwh = max(0.0, (target - soc) / 100 * self.ev_conf[CONF_EV_BATTERY_KWH])
            except (AttributeError, TypeError, ValueError):
                need_kwh = None
        if need_kwh is not None:
            energy = need_kwh / self.ev_conf[CONF_EV_EFFICIENCY]
        else:
            # No car SoC: "EV energy to add" is charger (AC) energy for this
            # session, minus what the charger already delivered.
            energy = float(self.settings[SET_EV_ENERGY_KWH])
            if entity := self.ev_conf.get(CONF_EV_SESSION_ENERGY):
                state = self.hass.states.get(entity)
                try:
                    delivered = float(state.state)  # type: ignore[union-attr]
                    info["session_kwh"] = delivered
                    energy = max(0.0, energy - delivered)
                except (AttributeError, TypeError, ValueError):
                    pass
        info["energy_needed_kwh"] = round(energy, 2)

        ready = datetime.strptime(str(self.settings[SET_EV_READY_BY])[:5], "%H:%M").time()
        local_now = now.astimezone(self.tz)
        deadline = datetime.combine(local_now.date(), ready, self.tz)
        if deadline <= local_now:
            deadline += timedelta(days=1)
        info["ready_by"] = deadline.isoformat()
        index = math.ceil((deadline - slot_starts[0]).total_seconds() / SLOT_SECONDS)

        if mode == EV_OFF or not connected or energy <= 0.05:
            return None, info
        return (
            EvRequest(
                mode=mode,
                energy_kwh=energy,
                deadline_index=index,
                min_kw=self.ev_conf[CONF_EV_MIN_KW],
                max_kw=self.ev_conf[CONF_EV_MAX_KW],
            ),
            info,
        )

    # ------------------------------------------------------------- planning

    async def _async_update_data(self) -> EmsData:
        try:
            return await self._run_cycle()
        except UpdateFailed:
            raise
        except Exception as err:
            _LOGGER.exception("Planning cycle failed")
            raise UpdateFailed(f"{type(err).__name__}: {err}") from err

    async def _run_cycle(self) -> EmsData:
        now = dt_util.utcnow()
        warnings: list[str] = []
        if self._last_load_sync is None or now - self._last_load_sync >= LOAD_SYNC_INTERVAL:
            try:
                await self._sync_load_history(2)
            except Exception as err:  # noqa: BLE001
                warnings.append(f"load history: {err}")
                self._last_load_sync = now

        count = self.conf[CONF_PLAN_DAYS] * 96
        slot_starts = slot_grid(now, count)
        fraction = max((slot_starts[0] + SLOT - now) / SLOT, 1 / 15)
        duration = np.full(count, SLOT_HOURS)
        duration[0] *= fraction

        bat_conf = self.conf[CONF_BATTERY]
        capacity = bat_conf[CONF_CAPACITY]
        soc = self._read_soc(warnings)
        raw, firm, origin = await self._prices(slot_starts, now, warnings)
        # Learn how good each forecast was (also while switched off, so it is
        # ready), then damp its swings by how far ahead it can be trusted and
        # widen the margin by its measured error.
        slot_ts = np.array([int(s.timestamp()) for s in slot_starts])
        self.price_skill.observe(now.timestamp(), slot_ts, raw, firm, origin, self.price_history)
        skill_margin = np.zeros(count)
        if self.settings[SET_FORECAST_TRUST]:
            raw, skill_margin = self.price_skill.adjust(now.timestamp(), slot_ts, raw, firm, origin)
        imp = self.tariff.import_price(raw)
        exp = self.tariff.export_price(raw)
        if until := self.conf[CONF_PRICES].get(CONF_NET_METERING_UNTIL):
            # Net metering (salderen): export earns the import price until the end date.
            end = datetime.combine(until, time(0), self.tz)
            netted = np.array([s < end for s in slot_starts])
            exp = np.where(netted, imp, exp)
        # The setting is the minimum margin; uncertain (far ahead) forecasts get more.
        margin = np.maximum(float(self.settings[SET_FORECAST_MARGIN]), skill_margin * (1 + self.tariff.vat))
        plain_imp = imp  # without the forecast margin, for the end-of-horizon value
        imp = np.where(firm, imp, imp + margin)
        exp = np.where(firm, exp, exp - margin)

        pv = await self._pv(slot_starts, now, warnings)
        load_conf = self.conf[CONF_LOAD]
        forecaster = LoadForecaster(
            self.load_history, self.tz, load_conf[CONF_DEFAULT_DAILY_KWH], max_slot_kwh=self._max_load_slot_kwh
        )
        self._history_days = forecaster.history_days
        if self.rejected_load:
            worst = max(self.rejected_load, key=self.rejected_load.get)
            warnings.append(
                f"ignored {len(self.rejected_load)} impossible load reading(s), largest "
                f"{self.rejected_load[worst]:.0f} kWh at {dt_util.as_local(dt_util.utc_from_timestamp(worst)):%Y-%m-%d %H:%M} "
                "(energy sensor reset or glitch)"
            )
        house = forecaster.forecast(slot_starts, now) * (1 + float(self.settings[SET_LOAD_MARGIN_PCT]) / 100)
        if forecaster.history_days < 7:
            warnings.append(f"load model has {forecaster.history_days} days of history (default profile used)")
            if problem := self._load_entity_problem():
                warnings.append(problem)

        self.heavy_loads = [h for h in self.heavy_loads if not h.expired(now)]
        self.manual_slots = [s for s in self.manual_slots if not s.expired(now)]
        fixed_heavy = np.zeros(count)
        flexible: list[HeavyLoad] = []
        for load in self.heavy_loads:
            if load.flexible:
                locked = load.chosen_start is not None and load.chosen_start - now <= FLEX_LOCK
                if locked:
                    fixed_heavy += load.profile(slot_starts, self.tz)
                else:
                    flexible.append(load)
            else:
                fixed_heavy += load.profile(slot_starts, self.tz)
        load = house + fixed_heavy
        load[0] *= fraction
        pv[0] *= fraction

        force_mode: list[str | None] = [None] * count
        force_power = np.full(count, np.nan)
        for slot in self.manual_slots:
            for i in np.flatnonzero(slot.mask(slot_starts, self.tz, now)):
                force_mode[i] = slot.mode
                force_power[i] = slot.power_kw if slot.power_kw is not None else np.nan

        allow_charge = np.full(count, bool(self.settings[SET_ALLOW_GRID_CHARGE])) & (
            imp <= float(self.settings[SET_MAX_CHARGE_PRICE])
        )
        allow_export = np.full(count, bool(self.settings[SET_ALLOW_BATTERY_EXPORT])) & (
            exp >= float(self.settings[SET_MIN_EXPORT_PRICE])
        )

        charge_eff = bat_conf[CONF_CHARGE_EFF]
        battery = BatteryConfig(
            capacity_kwh=capacity,
            reserve_kwh=capacity * float(self.settings[SET_RESERVE_PCT]) / 100,
            hold_kwh=capacity * float(self.settings[SET_HOLD_PCT]) / 100,
            hold_cost=float(self.settings[SET_HOLD_COST]),
            max_soc_kwh=capacity * float(self.settings[SET_MAX_SOC_PCT]) / 100,
            max_charge_kw=float(self.settings[SET_MAX_CHARGE_KW]),
            max_discharge_kw=float(self.settings[SET_MAX_DISCHARGE_KW]),
            max_export_kw=bat_conf.get(CONF_MAX_EXPORT_KW),
            charge_efficiency=charge_eff,
            discharge_efficiency=bat_conf[CONF_DISCHARGE_EFF],
            cycle_cost=float(self.settings[SET_CYCLE_COST]),
            step_kwh=bat_conf[CONF_SOC_STEP],
        )
        grid_conf = self.conf[CONF_GRID]
        grid = GridConfig(
            import_limit_kw=grid_conf[CONF_IMPORT_LIMIT],
            export_limit_kw=grid_conf[CONF_EXPORT_LIMIT],
            allow_curtail=grid_conf[CONF_ALLOW_CURTAIL],
        )
        end_value = bat_conf.get(CONF_TERMINAL_VALUE)
        if end_value is None:
            # The margin makes forecasts less attractive; it must not make energy
            # left at the end look more valuable (that would favour buying now).
            end_value = terminal_value(plain_imp, charge_eff)

        ev_request, ev_info = self._ev_request(slot_starts, now)
        problem = _Problem(
            slot_starts=slot_starts,
            now=now,
            tz=self.tz,
            inp=PlanInput(
                duration_h=duration,
                load_kwh=load,
                pv_kwh=pv,
                import_price=imp,
                export_price=exp,
                initial_soc_kwh=soc,
                terminal_value=end_value,
                allow_grid_charge=allow_charge,
                allow_battery_export=allow_export,
                force_mode=force_mode,
                force_power_kw=force_power,
                trade_hurdle=trade_hurdle(float(self.settings[SET_AGGRESSIVENESS])),
            ),
            bat=battery,
            grid=grid,
            flexible=flexible,
            ev=ev_request,
            ev_allow_battery=bool(self.settings[SET_EV_ALLOW_BATTERY]),
        )
        plan, ev_kwh, chosen = await self.hass.async_add_executor_job(_solve, problem)
        for load_item in flexible:
            load_item.chosen_start = chosen.get(load_item.id)
        self.schedule_save()

        data = self._build_data(now, slot_starts, plan, problem, raw, firm, origin, ev_kwh, ev_info, warnings)
        await self._apply_control(now, data)
        await self._apply_ev(now, data)
        return data

    # ------------------------------------------------------------ reporting

    def _build_data(
        self,
        now: datetime,
        slot_starts: list[datetime],
        plan: PlanResult,
        problem: _Problem,
        raw: np.ndarray,
        firm: np.ndarray,
        origin: np.ndarray,
        ev_kwh: np.ndarray,
        ev_info: dict,
        warnings: list[str],
    ) -> EmsData:
        inp = problem.inp
        capacity = problem.bat.capacity_kwh
        total_load = plan.extra.get("load_kwh", inp.load_kwh)
        # Heavy loads per slot (flexible ones at the start chosen this run), so the plan can mark them.
        heavy_kwh = np.zeros(len(slot_starts))
        heavy_names: list[list[str]] = [[] for _ in slot_starts]
        for load in self.heavy_loads:
            profile = load.profile(slot_starts, self.tz)
            heavy_kwh += profile
            for i in np.flatnonzero(profile > 0.005):
                heavy_names[i].append(load.name)
        heavy_kwh = np.minimum(heavy_kwh, total_load)
        slots = []
        for i, start in enumerate(slot_starts):
            slots.append(
                {
                    "start": start.astimezone(self.tz).isoformat(),
                    "mode": plan.mode[i],
                    "power_kw": round(float(plan.power_kw[i]), 2),
                    "soc": round(float(plan.soc_kwh[i + 1] / capacity * 100), 1),
                    "battery_kwh": round(float(plan.battery_kwh[i]), 2),
                    "grid_kwh": round(float(plan.grid_kwh[i]), 2),
                    "load_kwh": round(float(total_load[i]), 2),
                    "pv_kwh": round(float(inp.pv_kwh[i]), 2),
                    "ev_kwh": round(float(ev_kwh[i]), 2),
                    "import_price": round(float(inp.import_price[i]), 4),
                    "export_price": round(float(inp.export_price[i]), 4),
                    "price_source": str(origin[i]),
                    "cost": round(float(plan.cost[i]), 3),
                }
            )
            if plan.curtail[i]:
                slots[-1]["curtail"] = True
            if inp.force_mode[i]:
                slots[-1]["manual"] = True
            if heavy_names[i]:
                slots[-1]["heavy"] = heavy_names[i]
                slots[-1]["heavy_kwh"] = round(float(heavy_kwh[i]), 2)

        days: dict[str, dict] = {}
        for i, start in enumerate(slot_starts):
            key = start.astimezone(self.tz).date().isoformat()
            d = days.setdefault(
                key,
                {
                    "date": key,
                    "cost": 0.0,
                    "import_kwh": 0.0,
                    "export_kwh": 0.0,
                    "grid_charge_kwh": 0.0,
                    "pv_kwh": 0.0,
                    "load_kwh": 0.0,
                    "ev_kwh": 0.0,
                    "heavy_kwh": 0.0,
                    "prices": [],
                    "min_soc": 100.0,
                },
            )
            d["cost"] += plan.cost[i]
            d["import_kwh"] += max(plan.grid_kwh[i], 0)
            d["export_kwh"] += max(-plan.grid_kwh[i], 0)
            if plan.mode[i] == MODE_CHARGE:
                d["grid_charge_kwh"] += max(plan.battery_kwh[i], 0)
            d["pv_kwh"] += inp.pv_kwh[i]
            d["load_kwh"] += total_load[i]
            d["ev_kwh"] += ev_kwh[i]
            d["heavy_kwh"] += heavy_kwh[i]
            d["prices"].append(inp.import_price[i])
            d["min_soc"] = min(d["min_soc"], plan.soc_kwh[i + 1] / capacity * 100)
            d["end_soc"] = plan.soc_kwh[i + 1] / capacity * 100
        day_list = []
        for d in days.values():
            prices = d.pop("prices")
            d["avg_import_price"] = float(np.mean(prices))
            day_list.append({k: round(float(v), 3) if isinstance(v, (float, np.floating)) else v for k, v in d.items()})

        current = self._current_slot(plan, slot_starts, capacity, inp)
        firm_until = None
        if firm.any():
            last = int(np.flatnonzero(firm)[-1])
            firm_until = (slot_starts[last] + SLOT).astimezone(self.tz).isoformat()

        ev_slots = [slot_starts[i].astimezone(self.tz).isoformat() for i in np.flatnonzero(ev_kwh > 0)]
        ev_info.update(
            {
                "charge_now": bool(ev_kwh[0] > 0),
                "power_kw": round(float(ev_kwh[0] / inp.duration_h[0]), 2) if ev_kwh[0] > 0 else 0.0,
                "planned_kwh": round(float(ev_kwh.sum()), 2),
                "next_start": ev_slots[0] if ev_slots else None,
                "slots": ev_slots,
                "allow_battery": bool(self.settings[SET_EV_ALLOW_BATTERY]),
            }
        )

        local_today = now.astimezone(self.tz).date()

        def per_day(values: np.ndarray) -> list[dict]:
            out: dict[str, float] = {}
            for i, start in enumerate(slot_starts):
                key = start.astimezone(self.tz).date().isoformat()
                out[key] = out.get(key, 0.0) + float(values[i])
            return [{"date": k, "kwh": round(v, 2)} for k, v in out.items()]

        today_mask = np.array([s.astimezone(self.tz).date() == local_today for s in slot_starts])
        heavy = []
        for load in self.heavy_loads:
            item = load.to_dict()
            item["flexible"] = load.flexible
            heavy.append(item)

        return EmsData(
            updated=now,
            current=current,
            slots=slots,
            days=day_list,
            totals={
                "plan_cost": round(plan.total_cost, 2),
                **{key: round(value, 2) for key, value in plan.extra["comparison"].items()},
                "terminal_value": round(inp.terminal_value, 4),
                "final_soc": round(float(plan.soc_kwh[-1] / capacity * 100), 1),
                "horizon_end": (slot_starts[-1] + SLOT).astimezone(self.tz).isoformat(),
            },
            ev=ev_info,
            prices={
                "import": round(float(inp.import_price[0]), 4),
                "export": round(float(inp.export_price[0]), 4),
                "raw": round(float(raw[0]), 4),
                "firm_until": firm_until,
                "sources": {
                    "day_ahead": self.price_source.name,
                    "forecasts": [
                        {
                            "name": s.name,
                            "calibration": [round(c, 4) for c in s.calibration],
                            "error": s.error,
                            "last_success": s.last_success.isoformat() if s.last_success else None,
                            "covers_until": max((p[1] or p[0]) for p in s.points).isoformat() if s.points else None,
                        }
                        for s in self.forecast_sources
                    ],
                    "history_slots": len(self.price_history),
                    "skill": self.price_skill.summary([s.name for s in self.forecast_sources]),
                },
            },
            pv={
                "today_remaining_kwh": round(float(inp.pv_kwh[today_mask].sum()), 2),
                "days": per_day(inp.pv_kwh),
                "source_updated": self.solar.fetched_at.isoformat() if self.solar.fetched_at else None,
            },
            load={
                "today_remaining_kwh": round(float(total_load[today_mask].sum()), 2),
                "days": per_day(total_load),
                "history_days": self._history_days,
                "rejected_readings": len(self.rejected_load),
                "excluded_periods": [
                    {
                        "start": dt_util.as_local(dt_util.utc_from_timestamp(a)).isoformat(),
                        "end": dt_util.as_local(dt_util.utc_from_timestamp(b)).isoformat(),
                    }
                    for a, b in self.load_excluded
                ],
            },
            heavy_loads=heavy,
            manual_slots=[s.to_dict() for s in self.manual_slots],
            warnings=warnings,
        )

    def _current_slot(self, plan: PlanResult, slot_starts, capacity: float, inp: PlanInput) -> dict:
        mode = plan.mode[0]
        end = 1
        while end < len(plan.mode) and plan.mode[end] == mode:
            end += 1
        power = float(plan.power_kw[0])
        if mode in (MODE_CHARGE, MODE_EXPORT):
            # Inverters want a steady power for the whole block, not 15 min steps.
            block = plan.power_kw[:end]
            power = float(block.max()) if len(block) else power
        target = float(plan.soc_kwh[end] / capacity * 100)
        soc = float(plan.soc_kwh[0] / capacity * 100)
        reserve = float(self.settings[SET_RESERVE_PCT])
        load = plan.extra.get("load_kwh", inp.load_kwh)
        deficit = float(load[0] - inp.pv_kwh[0]) > 0.01
        control = mode
        if mode in (MODE_CHARGE, MODE_EXPORT) and power < IDLE_POWER_KW:
            # A 0 W charge/export ("PV to grid") is a battery at rest: hold.
            # A charge or discharge command would count its target as reached
            # and fall back to the inverter's own self use.
            control = MODE_HOLD
        elif mode == MODE_SELF_CONSUMPTION and deficit and plan.battery_kwh[0] > -0.01:
            # Self consumption that the plan keeps from discharging (at the
            # reserve): the inverter's own self use would carry on draining.
            control = MODE_HOLD
        return {
            "mode": mode,
            "control_mode": control,
            "power_kw": round(power, 2),
            "target_soc": round(target, 1),
            "stop_soc": _stop_soc(control, target, soc, reserve, float(self.settings[SET_MAX_SOC_PCT])),
            "soc": round(float(plan.soc_kwh[0] / capacity * 100), 1),
            "until": (slot_starts[end - 1] + SLOT).astimezone(self.tz).isoformat(),
            "curtail": bool(plan.curtail[0]),
            "import_price": round(float(inp.import_price[0]), 4),
            "export_price": round(float(inp.export_price[0]), 4),
            "reserve_soc": float(self.settings[SET_RESERVE_PCT]),
            "hold_soc": float(self.settings[SET_HOLD_PCT]),
        }

    # -------------------------------------------------------------- control

    async def _apply_control(self, now: datetime, data: EmsData) -> None:
        if not self.settings[SET_CONTROL_ENABLED]:
            return
        current = data.current
        mode = current["control_mode"]
        power_kw = current["power_kw"] if mode in (MODE_CHARGE, MODE_EXPORT) else 0.0
        script = self._scripts.get(mode)
        if script is None and mode in (MODE_HOLD, MODE_EXPORT):
            script = self._scripts.get(MODE_SELF_CONSUMPTION)
        if script is None:
            data.warnings.append(f"no control actions configured for mode '{mode}'")
            return
        stop = current["stop_soc"] if mode in (MODE_CHARGE, MODE_EXPORT) else None
        key = (mode, round(power_kw * 2) / 2, current["curtail"], stop)
        reapply = timedelta(minutes=self.conf[CONF_CONTROL].get(CONF_REAPPLY_MINUTES, 15))
        if key == self._last_control and self._last_control_at and now - self._last_control_at < reapply:
            return
        variables = {
            "mode": mode,
            "power_kw": power_kw,
            "power_w": int(round(power_kw * 1000)),
            "target_soc": current["stop_soc"],
            "plan_mode": current["mode"],
            "soc": current["soc"],
            "reserve_soc": current["reserve_soc"],
            "hold_soc": current["hold_soc"],
            "curtail": current["curtail"],
            "until": current["until"],
            "import_price": current["import_price"],
            "export_price": current["export_price"],
        }
        _LOGGER.info("Applying battery mode %s (%s)", mode, variables)
        self._last_control = key
        self._last_control_at = now
        self.hass.async_create_task(script.async_run(variables, context=Context()))

    async def _apply_ev(self, now: datetime, data: EmsData) -> None:
        if not self.settings[SET_CONTROL_ENABLED] or not self.ev_conf:
            return
        ev = data.ev
        if ev.get("mode") == EV_OFF or not ev.get("connected"):
            return
        charge = ev["charge_now"]
        script = self._ev_scripts.get(CONF_EV_START if charge else CONF_EV_STOP)
        if script is None:
            return
        power_kw = ev["power_kw"] if charge else 0.0
        key = (charge, round(power_kw, 1))
        reapply = timedelta(minutes=self.conf[CONF_CONTROL].get(CONF_REAPPLY_MINUTES, 15))
        if key == self._last_ev and self._last_ev_at and now - self._last_ev_at < reapply:
            return
        phases = self.ev_conf[CONF_EV_PHASES]
        voltage = self.ev_conf[CONF_EV_VOLTAGE]
        variables = {
            "power_kw": power_kw,
            "power_w": int(round(power_kw * 1000)),
            "current_a": round(power_kw * 1000 / (voltage * phases), 1),
            "phases": phases,
        }
        self._last_ev = key
        self._last_ev_at = now
        self.hass.async_create_task(script.async_run(variables, context=Context()))
