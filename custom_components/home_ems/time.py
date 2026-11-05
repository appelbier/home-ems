"""EV ready-by time."""

from __future__ import annotations

from datetime import datetime, time

from homeassistant.components.time import TimeEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import DOMAIN, SET_EV_READY_BY
from .coordinator import EmsCoordinator
from .entity import EmsSettingEntity


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
        async_add_entities([EvReadyBy(coordinator)])


class EvReadyBy(EmsSettingEntity, TimeEntity):
    def __init__(self, coordinator: EmsCoordinator) -> None:
        super().__init__(coordinator, SET_EV_READY_BY, "EV ready by", "mdi:clock-check")

    @property
    def native_value(self) -> time:
        return datetime.strptime(str(self.setting)[:5], "%H:%M").time()

    async def async_set_value(self, value: time) -> None:
        await self.async_set(value.strftime("%H:%M"))
