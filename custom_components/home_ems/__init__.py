"""Home EMS: week-ahead battery, EV and load planner for Home Assistant."""

from __future__ import annotations

from datetime import datetime
import hashlib
import logging
from pathlib import Path

import voluptuous as vol

from homeassistant.components.http import StaticPathConfig
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, discovery
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.typing import ConfigType

from .config_schema import CONFIG_SCHEMA, weekdays  # noqa: F401 - CONFIG_SCHEMA is read by HA
from .const import (
    CONF_DURATION_MIN,
    CONF_EARLIEST,
    CONF_END,
    CONF_END_TIME,
    CONF_ID,
    CONF_LATEST,
    CONF_MODE,
    CONF_NAME,
    CONF_POWER_KW,
    CONF_START,
    CONF_START_TIME,
    CONF_WEEKDAYS,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import EmsCoordinator
from .models import HeavyLoad, ManualSlot
from .optimizer import MODES

_LOGGER = logging.getLogger(__name__)

SERVICE_ADD_SLOT = "add_manual_slot"
SERVICE_REMOVE_SLOT = "remove_manual_slot"
SERVICE_CLEAR_SLOTS = "clear_manual_slots"
SERVICE_ADD_LOAD = "add_heavy_load"
SERVICE_REMOVE_LOAD = "remove_heavy_load"
SERVICE_CLEAR_LOADS = "clear_heavy_loads"
SERVICE_RECALCULATE = "recalculate"
SERVICE_REFRESH = "refresh_forecasts"
SERVICE_EXCLUDE_LOAD = "exclude_load_history"
SERVICE_CLEAR_EXCLUSIONS = "clear_load_exclusions"

FRONTEND_DIR = Path(__file__).parent / "frontend"
CARD_URL_PATH = "/home_ems"
CARD_FILES = ("home-ems-plan-card.js", "home-ems-schedule-card.js")

ADD_SLOT_SCHEMA = vol.Schema(
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
ADD_LOAD_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): cv.string,
        vol.Required(CONF_POWER_KW): vol.Coerce(float),
        vol.Required(CONF_DURATION_MIN): vol.All(vol.Coerce(int), vol.Range(5, 24 * 60)),
        vol.Optional(CONF_START): cv.datetime,
        vol.Optional(CONF_WEEKDAYS): weekdays,
        vol.Optional(CONF_START_TIME): cv.time,
        vol.Optional(CONF_EARLIEST): cv.datetime,
        vol.Optional(CONF_LATEST): cv.datetime,
    }
)
ID_SCHEMA = vol.Schema({vol.Required(CONF_ID): cv.string})
EXCLUDE_SCHEMA = vol.Schema({vol.Required(CONF_START): cv.datetime, vol.Required(CONF_END): cv.datetime})
CLEAR_SCHEMA = vol.Schema({vol.Optional("include_yaml", default=False): cv.boolean})


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up Home EMS from configuration.yaml."""
    if DOMAIN not in config:
        return True
    conf = config[DOMAIN]
    coordinator = EmsCoordinator(hass, conf)
    await coordinator.async_setup()
    hass.data[DOMAIN] = coordinator

    _register_services(hass, coordinator)
    await _async_register_card(hass)
    for platform in PLATFORMS:
        hass.async_create_task(discovery.async_load_platform(hass, platform, DOMAIN, {}, config))

    async def _started(_hass: HomeAssistant) -> None:
        # Price, Solcast and SoC entities from other integrations exist by now.
        await coordinator.async_bootstrap_history()
        await coordinator.async_refresh()
        if not coordinator.last_update_success:
            coordinator.async_refresh_when_soc_available()

    async_at_started(hass, _started)

    async def _stop(_event) -> None:
        coordinator.async_shutdown_listeners()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _stop)
    return True


async def _async_register_card(hass: HomeAssistant) -> None:
    """Serve the cards and load them on every dashboard (no resource to add)."""
    if hass.http is None or "frontend" not in hass.config.components:
        return
    from homeassistant.components.frontend import add_extra_js_url  # noqa: PLC0415

    def versions() -> dict[str, str]:
        return {name: hashlib.md5((FRONTEND_DIR / name).read_bytes()).hexdigest()[:8] for name in CARD_FILES}

    hashes = await hass.async_add_executor_job(versions)
    await hass.http.async_register_static_paths(
        [StaticPathConfig(f"{CARD_URL_PATH}/{name}", str(FRONTEND_DIR / name), False) for name in CARD_FILES]
    )
    for name, version in hashes.items():
        add_extra_js_url(hass, f"{CARD_URL_PATH}/{name}?v={version}")


def _register_services(hass: HomeAssistant, coordinator: EmsCoordinator) -> None:
    def aware(value: datetime | None) -> datetime | None:
        return coordinator._aware(value)  # noqa: SLF001 - shared helper

    async def add_slot(call: ServiceCall) -> ServiceResponse:
        data = call.data
        has_dates = CONF_START in data and CONF_END in data
        has_recurring = CONF_WEEKDAYS in data and CONF_START_TIME in data and CONF_END_TIME in data
        if not (has_dates or has_recurring):
            raise ServiceValidationError("give start + end, or weekdays + start_time + end_time")
        if has_dates and aware(data[CONF_END]) <= aware(data[CONF_START]):
            raise ServiceValidationError("end must be after start")
        if has_dates:
            slot = ManualSlot(
                mode=data[CONF_MODE],
                power_kw=data.get(CONF_POWER_KW),
                start=aware(data[CONF_START]),
                end=aware(data[CONF_END]),
                name=data.get(CONF_NAME, ""),
            )
        else:
            slot = ManualSlot(
                mode=data[CONF_MODE],
                power_kw=data.get(CONF_POWER_KW),
                weekdays=data[CONF_WEEKDAYS],
                start_time=data[CONF_START_TIME],
                end_time=data[CONF_END_TIME],
                name=data.get(CONF_NAME, ""),
            )
        coordinator.add_manual_slot(slot)
        await coordinator.async_request_refresh()
        return {"id": slot.id}

    async def remove_slot(call: ServiceCall) -> None:
        if not coordinator.remove_manual_slot(call.data[CONF_ID]):
            raise ServiceValidationError(f"no manual slot with id {call.data[CONF_ID]}")
        await coordinator.async_request_refresh()

    async def clear_slots(call: ServiceCall) -> None:
        coordinator.clear_manual_slots(call.data["include_yaml"])
        await coordinator.async_request_refresh()

    async def add_load(call: ServiceCall) -> ServiceResponse:
        data = call.data
        fixed = CONF_START in data
        recurring = CONF_WEEKDAYS in data and CONF_START_TIME in data
        flexible = CONF_EARLIEST in data and CONF_LATEST in data
        if sum((fixed, recurring, flexible)) != 1:
            raise ServiceValidationError("give exactly one of: start, weekdays + start_time, earliest + latest")
        load = HeavyLoad(
            name=data[CONF_NAME],
            power_kw=data[CONF_POWER_KW],
            duration_min=data[CONF_DURATION_MIN],
            start=aware(data.get(CONF_START)),
            weekdays=data.get(CONF_WEEKDAYS) or [],
            start_time=data.get(CONF_START_TIME),
            earliest=aware(data.get(CONF_EARLIEST)),
            latest=aware(data.get(CONF_LATEST)),
        )
        if flexible and load.latest - load.earliest < load.duration:
            raise ServiceValidationError("the window is shorter than the load duration")
        coordinator.add_heavy_load(load)
        await coordinator.async_request_refresh()
        return {"id": load.id}

    async def remove_load(call: ServiceCall) -> None:
        if not coordinator.remove_heavy_load(call.data[CONF_ID]):
            raise ServiceValidationError(f"no heavy load with id {call.data[CONF_ID]}")
        await coordinator.async_request_refresh()

    async def clear_loads(call: ServiceCall) -> None:
        coordinator.clear_heavy_loads(call.data["include_yaml"])
        await coordinator.async_request_refresh()

    async def recalculate(_call: ServiceCall) -> None:
        await coordinator.async_refresh()

    async def refresh(_call: ServiceCall) -> None:
        await coordinator.async_refresh_forecasts()

    async def exclude_load(call: ServiceCall) -> ServiceResponse:
        start, end = aware(call.data[CONF_START]), aware(call.data[CONF_END])
        if end <= start:
            raise ServiceValidationError("end must be after start")
        removed = coordinator.exclude_load_history(start, end)
        await coordinator.async_request_refresh()
        return {"removed_slots": removed}

    async def clear_exclusions(_call: ServiceCall) -> None:
        await coordinator.async_clear_load_exclusions()
        await coordinator.async_request_refresh()

    hass.services.async_register(DOMAIN, SERVICE_ADD_SLOT, add_slot, ADD_SLOT_SCHEMA, SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_REMOVE_SLOT, remove_slot, ID_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_CLEAR_SLOTS, clear_slots, CLEAR_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_ADD_LOAD, add_load, ADD_LOAD_SCHEMA, SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_REMOVE_LOAD, remove_load, ID_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_CLEAR_LOADS, clear_loads, CLEAR_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_RECALCULATE, recalculate)
    hass.services.async_register(DOMAIN, SERVICE_REFRESH, refresh)
    hass.services.async_register(
        DOMAIN, SERVICE_EXCLUDE_LOAD, exclude_load, EXCLUDE_SCHEMA, SupportsResponse.OPTIONAL
    )
    hass.services.async_register(DOMAIN, SERVICE_CLEAR_EXCLUSIONS, clear_exclusions)
