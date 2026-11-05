"""Number entities for the planner's tunable settings."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import (
    DOMAIN,
    SET_AGGRESSIVENESS,
    SET_CYCLE_COST,
    SET_EV_ENERGY_KWH,
    SET_EV_TARGET_SOC,
    SET_FORECAST_MARGIN,
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
)
from .coordinator import EmsCoordinator
from .entity import EmsSettingEntity

EUR_KWH = "EUR/kWh"


@dataclass(frozen=True)
class NumberSpec:
    key: str
    name: str
    unit: str | None
    minimum: float
    maximum: float
    step: float
    icon: str
    ev: bool = False


NUMBERS = (
    NumberSpec(SET_MAX_CHARGE_KW, "Max charge power", UnitOfPower.KILO_WATT, 0, 50, 0.1, "mdi:battery-arrow-up"),
    NumberSpec(SET_MAX_DISCHARGE_KW, "Max discharge power", UnitOfPower.KILO_WATT, 0, 50, 0.1, "mdi:battery-arrow-down"),
    NumberSpec(SET_RESERVE_PCT, "Reserve SoC", PERCENTAGE, 0, 100, 1, "mdi:battery-lock"),
    NumberSpec(SET_HOLD_PCT, "Hold SoC", PERCENTAGE, 0, 100, 1, "mdi:battery-medium"),
    NumberSpec(SET_HOLD_COST, "Hold strength", "EUR/kWh/day", 0, 2, 0.01, "mdi:battery-heart-outline"),
    NumberSpec(SET_MAX_SOC_PCT, "Max SoC", PERCENTAGE, 10, 100, 1, "mdi:battery-high"),
    NumberSpec(SET_MAX_CHARGE_PRICE, "Max grid charge price", EUR_KWH, -1, 2, 0.005, "mdi:cash-lock"),
    NumberSpec(SET_MIN_EXPORT_PRICE, "Min battery export price", EUR_KWH, -1, 2, 0.005, "mdi:cash-plus"),
    NumberSpec(SET_AGGRESSIVENESS, "Trading aggressiveness", PERCENTAGE, 0, 100, 5, "mdi:speedometer"),
    NumberSpec(SET_CYCLE_COST, "Battery wear cost", EUR_KWH, 0, 0.5, 0.001, "mdi:battery-heart-variant"),
    NumberSpec(SET_FORECAST_MARGIN, "Forecast price margin", EUR_KWH, 0, 0.2, 0.001, "mdi:chart-bell-curve"),
    NumberSpec(SET_PV_PESSIMISM, "PV pessimism", None, 0, 1, 0.05, "mdi:weather-cloudy"),
    NumberSpec(SET_LOAD_MARGIN_PCT, "Load margin", PERCENTAGE, -50, 100, 1, "mdi:home-plus"),
    NumberSpec(SET_EV_TARGET_SOC, "EV target SoC", PERCENTAGE, 0, 100, 1, "mdi:car-electric", ev=True),
    NumberSpec(SET_EV_ENERGY_KWH, "EV energy to add", UnitOfEnergy.KILO_WATT_HOUR, 0, 200, 0.5, "mdi:ev-station", ev=True),
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
    async_add_entities(SettingNumber(coordinator, spec) for spec in NUMBERS if coordinator.ev_conf or not spec.ev)


class SettingNumber(EmsSettingEntity, NumberEntity):
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: EmsCoordinator, spec: NumberSpec) -> None:
        super().__init__(coordinator, spec.key, spec.name, spec.icon)
        self._attr_native_unit_of_measurement = spec.unit
        self._attr_native_min_value = spec.minimum
        self._attr_native_max_value = spec.maximum
        self._attr_native_step = spec.step

    @property
    def native_value(self) -> float:
        return float(self.setting)

    async def async_set_native_value(self, value: float) -> None:
        await self.async_set(value)
