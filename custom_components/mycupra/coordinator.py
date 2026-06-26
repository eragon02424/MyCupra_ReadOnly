"""DataUpdateCoordinator für die MyCupra (Read-Only) Integration."""

from __future__ import annotations

import io
import json
import logging
import zipfile
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_DEVICE_NAME,
    CONF_REQUEST_IDENTIFIER,
    CONF_UPDATE_INTERVAL_MINUTES,
    CONF_VIN,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
)
from .cupra_client import CupraClient, CupraLoginError, CupraPermanentError

_LOGGER = logging.getLogger(__name__)


class MyCupraCoordinator(DataUpdateCoordinator[dict]):
    """Holt periodisch die neueste Datendatei vom EU Data Act Portal."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self.device_name = entry.data[CONF_DEVICE_NAME]
        self.vin = entry.data[CONF_VIN]

        self.client = CupraClient(
            email=entry.data["email"],
            password=entry.data["password"],
            vin=self.vin,
            request_identifier=entry.data[CONF_REQUEST_IDENTIFIER],
        )

        update_interval_minutes = entry.data.get(
            CONF_UPDATE_INTERVAL_MINUTES, DEFAULT_UPDATE_INTERVAL_MINUTES
        )

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{self.vin}",
            update_interval=timedelta(minutes=update_interval_minutes),
        )

    async def _async_update_data(self) -> dict:
        try:
            raw_bytes, filename = await self.hass.async_add_executor_job(
                self.client.download_latest
            )
        except CupraPermanentError as err:
            raise UpdateFailed(f"Dauerhafter Fehler, Konfiguration prüfen: {err}") from err
        except CupraLoginError as err:
            raise UpdateFailed(f"Datenabruf fehlgeschlagen: {err}") from err

        parsed = await self.hass.async_add_executor_job(
            self._parse_zip, raw_bytes, filename
        )
        _LOGGER.debug("Geparste Felder: %s", list(parsed.keys()))
        return parsed

    @staticmethod
    def _parse_zip(raw_bytes: bytes, filename: str) -> dict:
        """Entpackt die ZIP und wertet die JSON-Datei aus.

        Antwortformat: {"vin": ..., "Data": [{"dataFieldName": ..., "value": ...}, ...]}
        Bei mehrfach vorkommenden Feldnamen wird der erste Wert verwendet.
        """
        result = {"_raw_filename": filename, "_raw_size_bytes": len(raw_bytes)}

        try:
            with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
                json_name = next(
                    (n for n in zf.namelist() if n.endswith(".json")), None
                )
                if not json_name:
                    _LOGGER.warning("Keine JSON-Datei in ZIP %s gefunden.", filename)
                    return result
                data = json.loads(zf.read(json_name))
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Fehler beim Entpacken/Parsen von %s: %s", filename, err)
            return result

        fields: dict[str, str] = {}
        for entry in data.get("Data", []):
            name = entry.get("dataFieldName", "")
            if name and name not in fields:
                fields[name] = entry.get("value", "")

        _LOGGER.debug("ZIP %s: %d eindeutige Felder", filename, len(fields))

        # --- Hilfsfunktionen ---
        def _float(key: str):
            v = fields.get(key)
            try:
                return float(v) if v is not None else None
            except (ValueError, TypeError):
                return None

        def _int(key: str):
            v = fields.get(key)
            try:
                return int(float(v)) if v is not None else None
            except (ValueError, TypeError):
                return None

        def _soc_from_energy() -> int | None:
            """SOC aus Energieinhalten berechnen falls battery_level_HV fehlt."""
            current = _float("energy_contents.current_energy_content.physical_value")
            maximum = _float("energy_contents.maximal_energy_content.physical_value")
            if current is not None and maximum and maximum > 0:
                return round(current / maximum * 100)
            return None

        # SOC: primär battery_level_HV.value, Fallback auf Energieinhalt-Berechnung
        soc_raw = _float("battery_level_HV.value")
        soc = round(soc_raw) if soc_raw is not None else _soc_from_energy()

        # Energieinhalt in kWh
        current_energy = _float("energy_contents.current_energy_content.physical_value")
        max_energy = _float("energy_contents.maximal_energy_content.physical_value")

        result.update({
            # Batterie
            "soc":                          soc,
            "current_energy_kwh":           round(current_energy / 1000, 2) if current_energy is not None else None,
            "max_energy_kwh":               round(max_energy / 1000, 2) if max_energy is not None else None,
            "target_soc":                   _int("settings.target_soc"),
            "battery_care_limit":           _int("battery_care_mode.charge_bcam_threshold"),
            # Fahrzeug
            "mileage_km":                   _int("mileage.value"),
            "outdoor_temperature":          _float("outdoor_temperature"),
            "min_temperature":              _float("min_temperature"),
            "max_temperature":              _float("max_temperature"),
            # Verbrauch
            "climatization_consumption":    _float("additional_consumptions.interior_climatization_consumption"),
            "residual_consumption":         _float("additional_consumptions.residual_consumption"),
            "ascent_consumption":           _float("slope_consumption_values.ascent_slope_consumption.physical_value"),
            "descent_consumption":          _float("slope_consumption_values.descent_slope_consumption.physical_value"),
            # Status (Text)
            "charge_mode":                  fields.get("settings.charge_mode_selection"),
            "update_reason":                fields.get("update_reason"),
            # Binary
            "locked":                       fields.get("locked") == "true" if fields.get("locked") is not None else None,
            # Zeitstempel (neuester car_captured_time Wert)
            "car_captured_at":              fields.get("car_captured_utc_timestamp"),
        })

        return result
