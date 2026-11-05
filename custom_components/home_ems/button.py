"""Buttons to recalculate the plan and refresh forecasts."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import DOMAIN
from .coordinator import EmsCoordinator


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    if discovery_info is None:
        return
    coordinator: EmsCoordinator = hass.data[DOMAIN]
    async_add_entities([RecalculateButton(coordinator), RefreshButton(coordinator)])


class _Button(ButtonEntity):
    _attr_should_poll = False

    def __init__(self, coordinator: EmsCoordinator, key: str, name: str, icon: str) -> None:
        self.coordinator = coordinator
        self._attr_unique_id = f"{DOMAIN}_{key}"
        self._attr_name = f"Home EMS {name}"
        self._attr_icon = icon


class RecalculateButton(_Button):
    def __init__(self, coordinator: EmsCoordinator) -> None:
        super().__init__(coordinator, "recalculate", "Recalculate", "mdi:refresh")

    async def async_press(self) -> None:
        await self.coordinator.async_refresh()


class RefreshButton(_Button):
    """Forces new price and Solcast data (uses a Solcast API call)."""

    def __init__(self, coordinator: EmsCoordinator) -> None:
        super().__init__(coordinator, "refresh_forecasts", "Refresh forecasts", "mdi:cloud-refresh")

    async def async_press(self) -> None:
        await self.coordinator.async_refresh_forecasts()
