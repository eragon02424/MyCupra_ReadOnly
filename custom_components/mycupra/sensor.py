"""Sensor-Plattform für die MyCupra (Read-Only) Integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfEnergy,
    UnitOfLength,
    UnitOfPower,
    UnitOfSpeed,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import MyCupraCoordinator


@dataclass(frozen=True)
class MyCupraSensorDescription(SensorEntityDescription):
    """Erweiterte SensorEntityDescription mit optionalem Icon."""


SENSOR_DESCRIPTIONS: tuple[MyCupraSensorDescription, ...] = (
    # --- Batterie ---
    MyCupraSensorDescription(
        key="soc",
        name="Akkustand",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:battery-charging",
    ),
    MyCupraSensorDescription(
        key="charge_power_kw",
        name="Ladeleistung",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:flash",
    ),
    MyCupraSensorDescription(
        key="charge_rate_km_h",
        name="Laderate",
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:speedometer",
    ),
    MyCupraSensorDescription(
        key="remaining_charge_min",
        name="Ladezeit verbleibend",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:timer-outline",
    ),
    MyCupraSensorDescription(
        key="target_soc",
        name="Ziel-Ladestand",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:battery-charging-100",
    ),
    MyCupraSensorDescription(
        key="battery_care_limit",
        name="Battery Care Limit",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:battery-heart",
    ),
    # --- Fahrzeug ---
    MyCupraSensorDescription(
        key="mileage_km",
        name="Kilometerstand",
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:counter",
    ),
    MyCupraSensorDescription(
        key="outdoor_temperature",
        name="Außentemperatur",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:thermometer",
    ),
    MyCupraSensorDescription(
        key="min_temperature",
        name="Temperatur Min (Klima)",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:thermometer-low",
    ),
    MyCupraSensorDescription(
        key="max_temperature",
        name="Temperatur Max (Klima)",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:thermometer-high",
    ),
    # --- Verbrauch ---
    MyCupraSensorDescription(
        key="climatization_consumption",
        name="Klimaverbrauch",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:air-conditioner",
    ),
    MyCupraSensorDescription(
        key="residual_consumption",
        name="Ruheverbrauch",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:sleep",
    ),
    MyCupraSensorDescription(
        key="ascent_consumption",
        name="Steigungsverbrauch",
        native_unit_of_measurement="Wh/km",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:slope-uphill",
    ),
    # --- Status ---
    MyCupraSensorDescription(
        key="charge_state",
        name="Ladestatus",
        icon="mdi:ev-station",
    ),
    MyCupraSensorDescription(
        key="charge_type",
        name="Ladetyp",
        icon="mdi:cable-data",
    ),
    MyCupraSensorDescription(
        key="charge_mode",
        name="Lademodus",
        icon="mdi:tune",
    ),
    MyCupraSensorDescription(
        key="update_reason",
        name="Aktualisierungsgrund",
        icon="mdi:information-outline",
        entity_registry_enabled_default=False,
    ),
    # --- Binary ---
    MyCupraSensorDescription(
        key="locked",
        name="Verriegelt",
        icon="mdi:car-key",
    ),
    # --- Zeitstempel ---
    MyCupraSensorDescription(
        key="car_captured_at",
        name="Datenstand Fahrzeug",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:clock-outline",
    ),
    # --- Rohdaten (deaktiviert) ---
    MyCupraSensorDescription(
        key="_raw_filename",
        name="Letzte Datei",
        entity_registry_enabled_default=False,
        icon="mdi:file-outline",
    ),
    MyCupraSensorDescription(
        key="_raw_size_bytes",
        name="Dateigröße",
        native_unit_of_measurement="B",
        entity_registry_enabled_default=False,
        icon="mdi:file-outline",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: MyCupraCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        MyCupraSensor(coordinator, description)
        for description in SENSOR_DESCRIPTIONS
    )


class MyCupraSensor(CoordinatorEntity[MyCupraCoordinator], SensorEntity):
    """Einzelner Sensor, der seinen Wert aus coordinator.data[key] liest.

    Der Coordinator hält den letzten bekannten Stand pro Feld selbst (persistent in
    .storage), deshalb ist keine RestoreEntity-Logik mehr nötig.
    """

    def __init__(
        self,
        coordinator: MyCupraCoordinator,
        description: MyCupraSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.vin}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.vin)},
            name=coordinator.device_name,
            manufacturer="Cupra",
            model="Tavascan",
        )

    @property
    def native_value(self) -> Any:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self.entity_description.key)
