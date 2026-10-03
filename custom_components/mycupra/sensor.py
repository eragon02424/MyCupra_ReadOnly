"""Sensor-Plattform für die MyCupra (Read-Only) Integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
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


_M = SensorStateClass.MEASUREMENT
_DIAG = EntityCategory.DIAGNOSTIC
_KWH = UnitOfEnergy.KILO_WATT_HOUR


def _d(key, name, icon, unit=None, dc=None, sc=None, cat=None, enabled=True):
    return MyCupraSensorDescription(
        key=key, name=name, icon=icon, native_unit_of_measurement=unit,
        device_class=dc, state_class=sc, entity_category=cat,
        entity_registry_enabled_default=enabled,
    )


SENSOR_DESCRIPTIONS: tuple[MyCupraSensorDescription, ...] = (
    # --- Batterie ---
    _d("soc", "Akkustand", "mdi:battery-charging", PERCENTAGE, SensorDeviceClass.BATTERY, _M),
    _d("range_km", "Reichweite", "mdi:map-marker-distance", UnitOfLength.KILOMETERS, SensorDeviceClass.DISTANCE, _M),
    _d("current_energy_kwh", "Akkuenergie", "mdi:battery-high", _KWH, SensorDeviceClass.ENERGY_STORAGE, _M),
    _d("max_energy_kwh", "Nutzbare Batteriekapazität", "mdi:battery-plus", _KWH, SensorDeviceClass.ENERGY_STORAGE, _M, _DIAG),
    _d("last_charge_energy_kwh", "Letzte Ladung (ca.)", "mdi:ev-plug-type2", _KWH, None, None),
    _d("charge_power_kw", "Ladeleistung", "mdi:flash", UnitOfPower.KILO_WATT, SensorDeviceClass.POWER, _M),
    _d("charge_rate_km_h", "Laderate", "mdi:speedometer", UnitOfSpeed.KILOMETERS_PER_HOUR, None, _M),
    _d("remaining_charge_min", "Ladezeit verbleibend", "mdi:timer-outline", UnitOfTime.MINUTES, SensorDeviceClass.DURATION, _M),
    _d("target_soc", "Ziel-Ladestand", "mdi:battery-charging-100", PERCENTAGE, None, _M),
    _d("battery_care_limit", "Battery Care Limit", "mdi:battery-heart", PERCENTAGE, None, _M),
    # --- Fahrzeug ---
    _d("mileage_km", "Kilometerstand", "mdi:counter", UnitOfLength.KILOMETERS, SensorDeviceClass.DISTANCE, SensorStateClass.TOTAL_INCREASING),
    _d("outdoor_temperature", "Außentemperatur", "mdi:thermometer", UnitOfTemperature.CELSIUS, SensorDeviceClass.TEMPERATURE, _M),
    _d("min_temperature", "Temperatur Min (Klima)", "mdi:thermometer-low", UnitOfTemperature.CELSIUS, SensorDeviceClass.TEMPERATURE, _M),
    _d("max_temperature", "Temperatur Max (Klima)", "mdi:thermometer-high", UnitOfTemperature.CELSIUS, SensorDeviceClass.TEMPERATURE, _M),
    # --- Verbrauch ---
    _d("climatization_consumption", "Klimaverbrauch", "mdi:air-conditioner", _KWH, SensorDeviceClass.ENERGY, _M),
    _d("residual_consumption", "Ruheverbrauch", "mdi:sleep", _KWH, SensorDeviceClass.ENERGY, _M),
    _d("ascent_consumption", "Steigungsverbrauch", "mdi:slope-uphill", "Wh/km", None, _M),
    _d("descent_consumption", "Gefälleverbrauch", "mdi:slope-downhill", "Wh/km", None, _M, None, False),
    # --- Status ---
    _d("charge_state", "Ladestatus", "mdi:ev-station"),
    _d("charge_type", "Ladetyp", "mdi:cable-data"),
    _d("charge_mode", "Lademodus", "mdi:tune"),
    _d("update_reason", "Aktualisierungsgrund", "mdi:information-outline", enabled=False),
    # --- Einstellungen (Diagnose) ---
    _d("max_charge_current_ac", "Max. Ladestrom AC", "mdi:current-ac", cat=_DIAG),
    _d("auto_unlock_ac", "Stecker automatisch entriegeln", "mdi:lock-open-variant", cat=_DIAG),
    _d("charge_mode_selection", "Ladeart (Einstellung)", "mdi:tune-variant", cat=_DIAG),
    # --- Binary ---
    _d("locked", "Verriegelt", "mdi:car-key"),
    # --- Zeitstempel ---
    _d("car_captured_at", "Datenstand Fahrzeug", "mdi:clock-outline", dc=SensorDeviceClass.TIMESTAMP),
    # --- Rohdaten (deaktiviert) ---
    _d("_raw_filename", "Letzte Datei", "mdi:file-outline", enabled=False),
    _d("_raw_size_bytes", "Dateigröße", "mdi:file-outline", "B", enabled=False),
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
