"""YAML configuration schema for ``home_ems:`` in configuration.yaml."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_ALLOW_CURTAIL,
    CONF_ATTRIBUTES,
    CONF_BATTERY,
    CONF_CAPACITY,
    CONF_CHARGE_EFF,
    CONF_CONNECTED_ENTITY,
    CONF_CONTROL,
    CONF_CYCLE_COST,
    CONF_DATA,
    CONF_DAYS,
    CONF_DEFAULT_DAILY_KWH,
    CONF_DISCHARGE_EFF,
    CONF_DURATION_MIN,
    CONF_EARLIEST,
    CONF_ENABLED,
    CONF_END,
    CONF_END_TIME,
    CONF_ENERGY_ENTITY,
    CONF_ENERGY_TAX,
    CONF_ENTITY,
    CONF_EV,
    CONNECTED_STATES,
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
    CONF_FORECASTS,
    CONF_FORECAST_MARGIN,
    CONF_GRID,
    CONF_HEADERS,
    CONF_HEAVY_LOADS,
    CONF_HISTORY_DAYS,
    CONF_HISTORY_SOURCE,
    CONF_HISTORY_WEEKS,
    CONF_HOLD_COST,
    CONF_HOLD_PCT,
    CONF_MAX_LOAD_KW,
    CONF_NET_METERING_UNTIL,
    CONF_IMPORT_LIMIT,
    CONF_IMPORT_MARKUP,
    CONF_INCLUDES_TAX,
    CONF_LATEST,
    CONF_LOAD,
    CONF_MANUAL_SLOTS,
    CONF_MAX_CHARGE_KW,
    CONF_MAX_DISCHARGE_KW,
    CONF_MAX_EXPORT_KW,
    CONF_MAX_SOC_PCT,
    CONF_MODE,
    CONF_NAME,
    CONF_PLAN_DAYS,
    CONF_POWER_KW,
    CONF_PRICES,
    CONF_PV_SCALE,
    CONF_REAPPLY_MINUTES,
    CONF_REFRESH_MINUTES,
    CONF_SCALE,
    CONF_RESERVE_PCT,
    CONF_SERVICE,
    CONF_SOC_ENTITY,
    CONF_SOC_STEP,
    CONF_SOC_UNIT,
    CONF_SOLAR,
    CONF_SOLCAST_API_KEY,
    CONF_SOLCAST_CALLS,
    CONF_SOLCAST_ENTITIES,
    CONF_SOLCAST_SITES,
    CONF_SOURCE,
    CONF_START,
    CONF_START_TIME,
    CONF_SUBTRACT_ENTITIES,
    CONF_TERMINAL_VALUE,
    CONF_TIME_KEY,
    CONF_UNIT,
    CONF_UPDATE_INTERVAL,
    CONF_URL,
    CONF_VALUE_KEY,
    CONF_VAT_PCT,
    CONF_WEEKDAYS,
    DOMAIN,
)
from .ev_planner import EV_MODES, EV_SMART
from .models import WEEKDAY_NAMES
from .optimizer import MODES
from .price_forecast import EXPORT_RAW, EXPORT_SAME_AS_IMPORT

UNITS = ("auto", "eur_kwh", "eur_mwh", "ct_kwh")


def weekday(value: Any) -> int:
    """``mon``/``monday``/``0`` -> 0 ... ``sun`` -> 6."""
    if isinstance(value, int) and 0 <= value <= 6:
        return value
    text = str(value).strip().lower()[:3]
    if text in WEEKDAY_NAMES:
        return WEEKDAY_NAMES.index(text)
    if text.isdigit() and 0 <= int(text) <= 6:
        return int(text)
    raise vol.Invalid(f"invalid weekday: {value}")


def weekdays(value: Any) -> list[int]:
    if isinstance(value, str):
        if value.lower() in ("all", "daily", "every day"):
            return list(range(7))
        if value.lower() == "weekdays":
            return [0, 1, 2, 3, 4]
        if value.lower() == "weekend":
            return [5, 6]
        value = [v for v in value.replace(" ", "").split(",") if v]
    return sorted({weekday(v) for v in cv.ensure_list(value)})


def _one_source(value: dict) -> dict:
    if sum(k in value for k in (CONF_ENTITY, CONF_SERVICE, CONF_URL)) != 1:
        raise vol.Invalid("a price source needs exactly one of: entity, service, url")
    return value


SOURCE_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Optional(CONF_NAME): cv.string,
            vol.Optional(CONF_ENTITY): cv.entity_id,
            vol.Optional(CONF_SERVICE): cv.service,
            vol.Optional(CONF_URL): cv.url,
            vol.Optional(CONF_ATTRIBUTES): vol.All(cv.ensure_list, [cv.string]),
            vol.Optional(CONF_DATA, default={}): dict,
            vol.Optional(CONF_DAYS, default=2): vol.All(int, vol.Range(1, 7)),
            vol.Optional(CONF_UNIT, default="auto"): vol.In(UNITS),
            vol.Optional(CONF_SCALE): vol.Coerce(float),
            vol.Optional(CONF_INCLUDES_TAX, default=False): cv.boolean,
            vol.Optional(CONF_HEADERS, default={}): {cv.string: cv.string},
            vol.Optional(CONF_REFRESH_MINUTES): vol.All(int, vol.Range(1, 1440)),
            vol.Optional(CONF_TIME_KEY): cv.string,
            vol.Optional(CONF_VALUE_KEY): cv.string,
        }
    ),
    _one_source,
)

BATTERY_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CAPACITY): vol.Coerce(float),
        vol.Required(CONF_SOC_ENTITY): cv.entity_id,
        vol.Optional(CONF_SOC_UNIT, default="percent"): vol.In(["percent", "kwh"]),
        vol.Optional(CONF_MAX_CHARGE_KW, default=10.0): vol.Coerce(float),
        vol.Optional(CONF_MAX_DISCHARGE_KW, default=10.0): vol.Coerce(float),
        # Cap on a forced export setpoint; self consumption may use max_discharge_kw.
        vol.Optional(CONF_MAX_EXPORT_KW): vol.Coerce(float),
        vol.Optional(CONF_CHARGE_EFF, default=0.95): vol.All(vol.Coerce(float), vol.Range(0.5, 1)),
        vol.Optional(CONF_DISCHARGE_EFF, default=0.95): vol.All(vol.Coerce(float), vol.Range(0.5, 1)),
        vol.Optional(CONF_RESERVE_PCT, default=10.0): vol.All(vol.Coerce(float), vol.Range(0, 100)),
        # Level the planner normally keeps the battery at (default: the reserve).
        vol.Optional(CONF_HOLD_PCT): vol.All(vol.Coerce(float), vol.Range(0, 100)),
        vol.Optional(CONF_HOLD_COST, default=0.10): vol.All(vol.Coerce(float), vol.Range(0, 10)),
        vol.Optional(CONF_MAX_SOC_PCT, default=100.0): vol.All(vol.Coerce(float), vol.Range(1, 100)),
        vol.Optional(CONF_CYCLE_COST, default=0.01): vol.Coerce(float),
        vol.Optional(CONF_SOC_STEP, default=0.25): vol.All(vol.Coerce(float), vol.Range(0.05, 5)),
        vol.Optional(CONF_TERMINAL_VALUE): vol.Coerce(float),
    }
)

GRID_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_IMPORT_LIMIT, default=17.0): vol.Coerce(float),
        vol.Optional(CONF_EXPORT_LIMIT, default=17.0): vol.Coerce(float),
        vol.Optional(CONF_ALLOW_CURTAIL, default=True): cv.boolean,
    }
)

PRICES_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_SOURCE): SOURCE_SCHEMA,
        vol.Optional(CONF_FORECASTS, default=[]): vol.All(cv.ensure_list, [SOURCE_SCHEMA]),
        vol.Optional(CONF_VAT_PCT, default=21.0): vol.Coerce(float),
        vol.Optional(CONF_ENERGY_TAX, default=0.0916): vol.Coerce(float),
        vol.Optional(CONF_IMPORT_MARKUP, default=0.02): vol.Coerce(float),
        vol.Optional(CONF_EXPORT_MODE, default=EXPORT_RAW): vol.In([EXPORT_RAW, EXPORT_SAME_AS_IMPORT]),
        vol.Optional(CONF_EXPORT_FEE, default=0.0): vol.Coerce(float),
        vol.Optional(CONF_EXPORT_VAT, default=False): cv.boolean,
        vol.Optional(CONF_FORECAST_MARGIN, default=0.01): vol.Coerce(float),
        vol.Optional(CONF_HISTORY_WEEKS, default=4): vol.All(int, vol.Range(1, 8)),
        vol.Optional(CONF_HISTORY_SOURCE): SOURCE_SCHEMA,
        vol.Optional(CONF_NET_METERING_UNTIL): cv.date,
    }
)

SOLAR_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_SOLCAST_API_KEY): cv.string,
        vol.Optional(CONF_SOLCAST_SITES, default=[]): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(CONF_SOLCAST_CALLS, default=8): vol.All(int, vol.Range(1, 50)),
        vol.Optional(CONF_SOLCAST_ENTITIES, default=[]): cv.entity_ids,
        vol.Optional(CONF_PV_SCALE, default=1.0): vol.Coerce(float),
    }
)

LOAD_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_ENERGY_ENTITY): cv.entity_id,
        vol.Optional(CONF_SUBTRACT_ENTITIES, default=[]): cv.entity_ids,
        vol.Optional(CONF_HISTORY_DAYS, default=28): vol.All(int, vol.Range(7, 90)),
        vol.Optional(CONF_DEFAULT_DAILY_KWH, default=15.0): vol.Coerce(float),
        # Highest believable house load; readings above it are meter glitches.
        vol.Optional(CONF_MAX_LOAD_KW, default=30.0): vol.All(vol.Coerce(float), vol.Range(1, 200)),
    }
)

EV_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_CONNECTED_ENTITY): cv.entity_id,
        vol.Optional(CONF_EV_SOC_ENTITY): cv.entity_id,
        vol.Optional(CONF_EV_SESSION_ENERGY): cv.entity_id,
        vol.Optional(CONF_EV_CONNECTED_STATES, default=list(CONNECTED_STATES)): vol.All(
            cv.ensure_list, [cv.string]
        ),
        vol.Optional(CONF_EV_BATTERY_KWH, default=60.0): vol.Coerce(float),
        vol.Optional(CONF_EV_MIN_KW, default=4.1): vol.Coerce(float),
        vol.Optional(CONF_EV_MAX_KW, default=11.0): vol.Coerce(float),
        vol.Optional(CONF_EV_PHASES, default=3): vol.In([1, 3]),
        vol.Optional(CONF_EV_VOLTAGE, default=230): vol.Coerce(float),
        vol.Optional(CONF_EV_EFFICIENCY, default=0.9): vol.All(vol.Coerce(float), vol.Range(0.5, 1)),
        vol.Optional(CONF_EV_TARGET_SOC, default=80.0): vol.Coerce(float),
        vol.Optional(CONF_EV_READY_BY, default="07:00"): cv.time,
        vol.Optional(CONF_EV_MODE, default=EV_SMART): vol.In(EV_MODES),
        vol.Optional(CONF_EV_START): cv.SCRIPT_SCHEMA,
        vol.Optional(CONF_EV_STOP): cv.SCRIPT_SCHEMA,
    }
)

CONTROL_SCHEMA = vol.Schema(
    {
        vol.Optional(mode): cv.SCRIPT_SCHEMA
        for mode in MODES
    }
    | {
        vol.Optional(CONF_REAPPLY_MINUTES, default=15): vol.All(int, vol.Range(1, 240)),
        vol.Optional(CONF_ENABLED, default=False): cv.boolean,
    }
)

MANUAL_SLOT_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_MODE): vol.In(MODES),
        vol.Optional(CONF_NAME, default=""): cv.string,
        vol.Optional(CONF_POWER_KW): vol.Coerce(float),
        vol.Optional(CONF_START): cv.datetime,
        vol.Optional(CONF_END): cv.datetime,
        vol.Optional(CONF_WEEKDAYS): weekdays,
        vol.Optional(CONF_START_TIME): cv.time,
        vol.Optional(CONF_END_TIME): cv.time,
    }
)

HEAVY_LOAD_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): cv.string,
        vol.Required(CONF_POWER_KW): vol.Coerce(float),
        vol.Required(CONF_DURATION_MIN): vol.All(int, vol.Range(5, 24 * 60)),
        vol.Optional(CONF_START): cv.datetime,
        vol.Optional(CONF_WEEKDAYS): weekdays,
        vol.Optional(CONF_START_TIME): cv.time,
        vol.Optional(CONF_EARLIEST): cv.datetime,
        vol.Optional(CONF_LATEST): cv.datetime,
    }
)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional(CONF_PLAN_DAYS, default=7): vol.All(int, vol.Range(1, 14)),
                vol.Optional(CONF_UPDATE_INTERVAL, default=5): vol.All(int, vol.Range(1, 60)),
                vol.Required(CONF_BATTERY): BATTERY_SCHEMA,
                vol.Optional(CONF_GRID, default={}): GRID_SCHEMA,
                vol.Required(CONF_PRICES): PRICES_SCHEMA,
                vol.Optional(CONF_SOLAR, default={}): SOLAR_SCHEMA,
                vol.Optional(CONF_LOAD, default={}): LOAD_SCHEMA,
                vol.Optional(CONF_EV): EV_SCHEMA,
                vol.Optional(CONF_CONTROL, default={}): CONTROL_SCHEMA,
                vol.Optional(CONF_MANUAL_SLOTS, default=[]): vol.All(cv.ensure_list, [MANUAL_SLOT_SCHEMA]),
                vol.Optional(CONF_HEAVY_LOADS, default=[]): vol.All(cv.ensure_list, [HEAVY_LOAD_SCHEMA]),
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)
