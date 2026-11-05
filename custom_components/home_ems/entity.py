"""Base entities for Home EMS."""

from __future__ import annotations

from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EmsCoordinator


class EmsEntity(CoordinatorEntity[EmsCoordinator]):
    """Entity that shows part of the latest plan."""

    def __init__(self, coordinator: EmsCoordinator, key: str, name: str, icon: str | None = None) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{key}"
        self._attr_name = f"Home EMS {name}"
        if icon:
            self._attr_icon = icon

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None


class EmsSettingEntity(Entity):
    """Entity backed by one runtime setting of the coordinator."""

    _attr_should_poll = False

    def __init__(self, coordinator: EmsCoordinator, key: str, name: str, icon: str | None = None) -> None:
        self.coordinator = coordinator
        self.key = key
        self._attr_unique_id = f"{DOMAIN}_setting_{key}"
        self._attr_name = f"Home EMS {name}"
        if icon:
            self._attr_icon = icon

    @property
    def setting(self):
        return self.coordinator.settings[self.key]

    async def async_set(self, value) -> None:
        await self.coordinator.async_set_setting(self.key, value)
        self.async_write_ha_state()
