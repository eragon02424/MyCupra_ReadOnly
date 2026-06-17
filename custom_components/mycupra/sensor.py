"""Sensor-Plattform für die MyCupra (Read-Only) Integration.

ACHTUNG: aktuell nur PLATZHALTER-Sensoren (Dateiname/Dateigröße der zuletzt
geladenen Datei), da coordinator._parse_zip() noch keine echte Auswertung
durchführt. Sobald die ZIP/JSON-Parsing-Logik separat entwickelt und getestet
wurde, werden hier die echten Sensoren (SOC, Ladezustand, Kilometerstand etc.)
ergänzt.
"""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import MyCupraCoordinator

SENSOR_DESCRIPTIONS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="_raw_filename",
        translation_key="last_file_name",
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="_raw_size_bytes",
        translation_key="last_file_size",
        native_unit_of_measurement="B",
        entity_registry_enabled_default=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Sensor-Entities für einen Config-Entry (= ein Fahrzeug) anlegen."""
    coordinator: MyCupraCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        MyCupraSensor(coordinator, description)
        for description in SENSOR_DESCRIPTIONS
    )


class MyCupraSensor(CoordinatorEntity[MyCupraCoordinator], SensorEntity):
    """Einzelner Sensor, der seinen Wert aus coordinator.data[key] liest."""

    def __init__(
        self,
        coordinator: MyCupraCoordinator,
        description: SensorEntityDescription,
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
    def native_value(self):
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self.entity_description.key)
