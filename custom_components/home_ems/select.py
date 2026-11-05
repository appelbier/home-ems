"""EV charge mode selector."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import DOMAIN, SET_EV_MODE
from .coordinator import EmsCoordinator
from .entity import EmsSettingEntity
from .ev_planner import EV_MODES


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    if discovery_info is None:
        return
    coordinator: EmsCoordinator = hass.data[DOMAIN]
    if coordinator.ev_conf:
        async_add_entities([EvModeSelect(coordinator)])


class EvModeSelect(EmsSettingEntity, SelectEntity):
    _attr_options = list(EV_MODES)

    def __init__(self, coordinator: EmsCoordinator) -> None:
        super().__init__(coordinator, SET_EV_MODE, "EV charge mode", "mdi:ev-plug-type2")

    @property
    def current_option(self) -> str:
        return str(self.setting)

    async def async_select_option(self, option: str) -> None:
        await self.async_set(option)
