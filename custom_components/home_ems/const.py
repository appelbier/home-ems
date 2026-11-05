"""Constants for the Home EMS integration."""

from __future__ import annotations

DOMAIN = "home_ems"
STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.data"

PLATFORMS = ["sensor", "switch", "number", "select", "time", "button"]

# Top-level configuration keys
CONF_PLAN_DAYS = "plan_days"
CONF_UPDATE_INTERVAL = "update_interval"
CONF_BATTERY = "battery"
CONF_GRID = "grid"
CONF_PRICES = "prices"
CONF_SOLAR = "solar"
CONF_LOAD = "load"
CONF_EV = "ev"
CONF_CONTROL = "control"
CONF_MANUAL_SLOTS = "manual_slots"
CONF_HEAVY_LOADS = "heavy_loads"

# Battery
CONF_CAPACITY = "capacity_kwh"
CONF_SOC_ENTITY = "soc_entity"
CONF_SOC_UNIT = "soc_unit"
CONF_MAX_CHARGE_KW = "max_charge_kw"
CONF_MAX_DISCHARGE_KW = "max_discharge_kw"
CONF_MAX_EXPORT_KW = "max_export_kw"
CONF_CHARGE_EFF = "charge_efficiency"
CONF_DISCHARGE_EFF = "discharge_efficiency"
CONF_RESERVE_PCT = "reserve_soc_pct"
CONF_HOLD_PCT = "hold_soc_pct"
CONF_HOLD_COST = "hold_cost"
CONF_MAX_SOC_PCT = "max_soc_pct"
CONF_CYCLE_COST = "cycle_cost"
CONF_SOC_STEP = "soc_step_kwh"
CONF_TERMINAL_VALUE = "terminal_value"

# Grid
CONF_IMPORT_LIMIT = "import_limit_kw"
CONF_EXPORT_LIMIT = "export_limit_kw"
CONF_ALLOW_CURTAIL = "allow_curtail"

# Prices
CONF_SOURCE = "source"
CONF_FORECASTS = "forecasts"
CONF_ENTITY = "entity"
CONF_SERVICE = "service"
CONF_URL = "url"
CONF_ATTRIBUTES = "attributes"
CONF_DATA = "data"
CONF_DAYS = "days"
CONF_UNIT = "unit"
CONF_INCLUDES_TAX = "includes_tax"
CONF_HEADERS = "headers"
CONF_REFRESH_MINUTES = "refresh_minutes"
CONF_TIME_KEY = "time_key"
CONF_VALUE_KEY = "value_key"
CONF_SCALE = "scale"  # explicit multiplier to EUR/kWh, overrides unit
CONF_NAME = "name"
CONF_VAT_PCT = "vat_pct"
CONF_ENERGY_TAX = "energy_tax"
CONF_IMPORT_MARKUP = "import_markup"
CONF_EXPORT_MODE = "export_mode"
CONF_EXPORT_FEE = "export_fee"
CONF_EXPORT_VAT = "export_vat"
CONF_FORECAST_MARGIN = "forecast_margin"
CONF_HISTORY_WEEKS = "history_weeks"
CONF_HISTORY_SOURCE = "history_source"
CONF_NET_METERING_UNTIL = "net_metering_until"

# Solar
CONF_SOLCAST_API_KEY = "solcast_api_key"
CONF_SOLCAST_SITES = "solcast_sites"
CONF_SOLCAST_CALLS = "api_calls_per_day"
CONF_SOLCAST_ENTITIES = "solcast_entities"
CONF_PV_SCALE = "scale"

# Load
CONF_ENERGY_ENTITY = "energy_entity"
CONF_SUBTRACT_ENTITIES = "subtract_entities"
CONF_HISTORY_DAYS = "history_days"
CONF_DEFAULT_DAILY_KWH = "default_daily_kwh"
CONF_MAX_LOAD_KW = "max_kw"

# EV
CONF_CONNECTED_ENTITY = "connected_entity"
CONF_EV_SOC_ENTITY = "soc_entity"
CONF_EV_SESSION_ENERGY = "session_energy_entity"
CONF_EV_CONNECTED_STATES = "connected_states"
CONF_EV_BATTERY_KWH = "battery_kwh"
CONF_EV_MIN_KW = "min_kw"
CONF_EV_MAX_KW = "max_kw"
CONF_EV_PHASES = "phases"
CONF_EV_VOLTAGE = "voltage"
CONF_EV_EFFICIENCY = "efficiency"
CONF_EV_TARGET_SOC = "target_soc_pct"
CONF_EV_READY_BY = "ready_by"
CONF_EV_MODE = "mode"
CONF_EV_START = "start"
CONF_EV_STOP = "stop"

# Control
CONF_REAPPLY_MINUTES = "reapply_minutes"
CONF_ENABLED = "enabled"

# Manual slots / heavy loads
CONF_MODE = "mode"
CONF_POWER_KW = "power_kw"
CONF_START = "start"
CONF_END = "end"
CONF_WEEKDAYS = "weekdays"
CONF_START_TIME = "start_time"
CONF_END_TIME = "end_time"
CONF_DURATION_MIN = "duration_min"
CONF_EARLIEST = "earliest"
CONF_LATEST = "latest"
CONF_ID = "id"

# Runtime settings exposed as number / switch / select / time entities.
SET_CONTROL_ENABLED = "control_enabled"
SET_ALLOW_GRID_CHARGE = "allow_grid_charge"
SET_ALLOW_BATTERY_EXPORT = "allow_battery_export"
SET_EV_ALLOW_BATTERY = "ev_allow_battery"
SET_MAX_CHARGE_KW = "max_charge_kw"
SET_MAX_DISCHARGE_KW = "max_discharge_kw"
SET_RESERVE_PCT = "reserve_soc_pct"
SET_HOLD_PCT = "hold_soc_pct"
SET_HOLD_COST = "hold_cost"
SET_MAX_SOC_PCT = "max_soc_pct"
SET_MAX_CHARGE_PRICE = "max_charge_price"
SET_MIN_EXPORT_PRICE = "min_export_price"
SET_CYCLE_COST = "cycle_cost"
SET_AGGRESSIVENESS = "trading_aggressiveness_pct"
SET_PV_PESSIMISM = "pv_pessimism"
SET_LOAD_MARGIN_PCT = "load_margin_pct"
SET_FORECAST_MARGIN = "forecast_price_margin"
SET_FORECAST_TRUST = "forecast_trust"
SET_EV_MODE = "ev_mode"
SET_EV_TARGET_SOC = "ev_target_soc_pct"
SET_EV_ENERGY_KWH = "ev_energy_kwh"
SET_EV_READY_BY = "ev_ready_by"

# Default "car plugged in" states: binary sensors, common wallbox integrations
# and OCPP connector statuses.
CONNECTED_STATES = (
    "on", "true", "connected", "plugged", "plugged_in", "charging", "home", "occupied",
    "preparing", "suspendedev", "suspendedevse", "finishing",
)

SIGNAL_SETTINGS = f"{DOMAIN}_settings"

ATTRIBUTION = "Home EMS week planner"
