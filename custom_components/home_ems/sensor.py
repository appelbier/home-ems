"""Sensors showing the plan, forecasts and current decision."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import EmsCoordinator, EmsData
from .entity import EmsEntity
from .optimizer import MODE_CHARGE, MODES

CURRENCY_PER_KWH = "EUR/kWh"


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    if discovery_info is None:
        return
    coordinator: EmsCoordinator = hass.data[DOMAIN]
    entities: list[SensorEntity] = [
        ModeSensor(coordinator),
        PlanSensor(coordinator),
        TargetSocSensor(coordinator),
        NextChargeSensor(coordinator),
        SavingsSensor(coordinator, "savings", "Planned savings", "mdi:piggy-bank"),
        SavingsSensor(coordinator, "battery_savings", "Battery savings", "mdi:home-battery"),
        PriceSensor(coordinator, "import"),
        PriceSensor(coordinator, "export"),
        ForecastSensor(coordinator, "pv", "PV forecast", "mdi:solar-power"),
        ForecastSensor(coordinator, "load", "Load forecast", "mdi:home-lightning-bolt"),
        ListSensor(coordinator, "heavy_loads", "Heavy loads", "mdi:washing-machine"),
        ListSensor(coordinator, "manual_slots", "Manual slots", "mdi:calendar-clock"),
    ]
    if coordinator.ev_conf:
        entities.append(EvSensor(coordinator))
    async_add_entities(entities)


class _DataSensor(EmsEntity, SensorEntity):
    @property
    def data(self) -> EmsData:
        return self.coordinator.data


class ModeSensor(_DataSensor):
    """What the battery should be doing right now."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(MODES)

    def __init__(self, coordinator: EmsCoordinator) -> None:
        super().__init__(coordinator, "mode", "Mode", "mdi:battery-sync")

    @property
    def native_value(self) -> str | None:
        return self.data.current["mode"] if self.data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.data:
            return {}
        return {
            **self.data.current,
            "control_enabled": self.coordinator.settings["control_enabled"],
            "updated": self.data.updated.isoformat(),
            "warnings": self.data.warnings,
        }


class PlanSensor(_DataSensor):
    """Cost of the whole plan; the slots are in the attributes for charts."""

    _attr_native_unit_of_measurement = "EUR"
    _attr_device_class = SensorDeviceClass.MONETARY
    _unrecorded_attributes = frozenset({"slots", "days"})

    def __init__(self, coordinator: EmsCoordinator) -> None:
        super().__init__(coordinator, "plan", "Plan", "mdi:chart-timeline-variant")

    @property
    def native_value(self) -> float | None:
        return self.data.totals["plan_cost"] if self.data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.data:
            return {}
        return {**self.data.totals, "days": self.data.days, "slots": self.data.slots}


class TargetSocSensor(_DataSensor):
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_device_class = SensorDeviceClass.BATTERY

    def __init__(self, coordinator: EmsCoordinator) -> None:
        super().__init__(coordinator, "target_soc", "Target SoC")

    @property
    def native_value(self) -> float | None:
        return self.data.current["target_soc"] if self.data else None


class NextChargeSensor(_DataSensor):
    """Start of the next grid charge block."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: EmsCoordinator) -> None:
        super().__init__(coordinator, "next_charge", "Next charge", "mdi:battery-charging")

    def _next(self) -> tuple[datetime | None, float]:
        if not self.data:
            return None, 0.0
        start, energy = None, 0.0
        for slot in self.data.slots:
            if slot["mode"] == MODE_CHARGE:
                start = start or dt_util.parse_datetime(slot["start"])
                energy += max(slot["battery_kwh"], 0)
            elif start is not None:
                break
        return start, energy

    @property
    def native_value(self) -> datetime | None:
        return self._next()[0]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"energy_kwh": round(self._next()[1], 2)}


class SavingsSensor(_DataSensor):
    """Planned savings over the horizon: versus plain self consumption, or versus no battery."""

    _attr_native_unit_of_measurement = "EUR"
    _attr_device_class = SensorDeviceClass.MONETARY

    NOTES = {
        "savings": "plan versus plain self consumption with the same battery",
        "battery_savings": "plan versus no battery at all",
    }

    def __init__(self, coordinator: EmsCoordinator, key: str, name: str, icon: str) -> None:
        super().__init__(coordinator, key, name, icon)
        self.key = key

    @property
    def native_value(self) -> float | None:
        return self.data.totals[self.key] if self.data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.data:
            return {}
        t = self.data.totals
        keys = ("plan_cost", "baseline_cost", "no_battery_cost", "wear_cost", "stored_value_change", "terminal_value")
        return {
            **{k: t[k] for k in keys},
            "note": f"{self.NOTES[self.key]}; net of battery wear, energy left in the battery "
            "at the end valued at terminal_value",
        }


class PriceSensor(_DataSensor):
    """Current consumer price with the full 15 minute forecast in attributes."""

    _attr_native_unit_of_measurement = CURRENCY_PER_KWH
    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset({"forecast"})

    def __init__(self, coordinator: EmsCoordinator, kind: str) -> None:
        super().__init__(coordinator, f"{kind}_price", f"{kind.capitalize()} price", "mdi:currency-eur")
        self.kind = kind

    @property
    def native_value(self) -> float | None:
        return self.data.prices[self.kind] if self.data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.data:
            return {}
        key = f"{self.kind}_price"
        return {
            "firm_until": self.data.prices["firm_until"],
            "sources": self.data.prices["sources"],
            "forecast": [{"start": s["start"], "price": s[key]} for s in self.data.slots],
        }


class ForecastSensor(_DataSensor):
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY

    def __init__(self, coordinator: EmsCoordinator, kind: str, name: str, icon: str) -> None:
        super().__init__(coordinator, f"{kind}_forecast", name, icon)
        self.kind = kind

    @property
    def native_value(self) -> float | None:
        return getattr(self.data, self.kind)["today_remaining_kwh"] if self.data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.data:
            return {}
        return dict(getattr(self.data, self.kind))


class ListSensor(_DataSensor):
    """Count of manual slots / heavy loads, the items in the attributes."""

    def __init__(self, coordinator: EmsCoordinator, kind: str, name: str, icon: str) -> None:
        super().__init__(coordinator, kind, name, icon)
        self.kind = kind

    @property
    def native_value(self) -> int | None:
        return len(getattr(self.data, self.kind)) if self.data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"items": getattr(self.data, self.kind)} if self.data else {}


class EvSensor(_DataSensor):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["charging", "planned", "idle", "not_connected", "off"]

    def __init__(self, coordinator: EmsCoordinator) -> None:
        super().__init__(coordinator, "ev", "EV", "mdi:car-electric")

    @property
    def native_value(self) -> str | None:
        if not self.data:
            return None
        ev = self.data.ev
        if ev.get("mode") == "off":
            return "off"
        if not ev.get("connected", True):
            return "not_connected"
        if ev.get("charge_now"):
            return "charging"
        return "planned" if ev.get("planned_kwh") else "idle"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return dict(self.data.ev) if self.data else {}
