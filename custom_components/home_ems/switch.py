"""Switches for the planner's on/off settings."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import (
    DOMAIN,
    SET_ALLOW_BATTERY_EXPORT,
    SET_ALLOW_GRID_CHARGE,
    SET_CONTROL_ENABLED,
    SET_EV_ALLOW_BATTERY,
    SET_FORECAST_TRUST,
)
from .coordinator import EmsCoordinator
from .entity import EmsSettingEntity

SWITCHES = (
    (SET_CONTROL_ENABLED, "Control inverter", "mdi:robot", False),
    (SET_ALLOW_GRID_CHARGE, "Allow grid charging", "mdi:transmission-tower-import", False),
    (SET_ALLOW_BATTERY_EXPORT, "Allow battery export", "mdi:transmission-tower-export", False),
    (SET_EV_ALLOW_BATTERY, "EV may use house battery", "mdi:car-battery", True),
    (SET_FORECAST_TRUST, "Automatic forecast trust", "mdi:chart-bell-curve-cumulative", False),
)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    if discovery_info is None:
        return
    coordinator: EmsCoordinator = hass.data[DOMAIN]
    async_add_entities(
        SettingSwitch(coordinator, key, name, icon)
        for key, name, icon, needs_ev in SWITCHES
        if coordinator.ev_conf or not needs_ev
    )


class SettingSwitch(EmsSettingEntity, SwitchEntity):
    @property
    def is_on(self) -> bool:
        return bool(self.setting)

    async def async_turn_on(self, **kwargs) -> None:
        await self.async_set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.async_set(False)
