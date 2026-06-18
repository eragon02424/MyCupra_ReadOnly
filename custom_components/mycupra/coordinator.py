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
        Bei mehrfach vorkommenden Feldnamen wird der erste Wert verwendet
        (Felder kommen in Gruppen vor, der Inhalt ist identisch).
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

        # --- Numerische Felder ---
        def _float(key: str):
            v = fields.get(key)
            try:
                return float(v) if v is not None else None
            except (ValueError, TypeError):
                return None

        def _int(key: str):
            v = fields.get(key)
            try:
                return int(v) if v is not None else None
            except (ValueError, TypeError):
                return None

        def _seconds_to_minutes(key: str):
            """Konvertiert '33000s' -> 550 (Minuten)."""
            v = fields.get(key)
            if v is None:
                return None
            try:
                return round(int(str(v).rstrip("s")) / 60)
            except (ValueError, TypeError):
                return None

        result.update({
            # Batterie
            "soc":                      _int("battery_state_report.soc"),
            "charge_power_kw":          _float("battery_state_report.charge_power"),
            "charge_rate_km_h":         _float("battery_state_report.charge_rate"),
            "remaining_charge_min":     _seconds_to_minutes("battery_state_report.remaining_charging_time_complete"),
            "target_soc":               _int("settings.target_soc"),
            "battery_care_limit":       _int("battery_care_mode.charge_bcam_threshold"),
            # Fahrzeug
            "mileage_km":               _int("mileage.value"),
            "outdoor_temperature":      _float("outdoor_temperature"),
            "min_temperature":          _float("min_temperature"),
            "max_temperature":          _float("max_temperature"),
            # Verbrauch
            "climatization_consumption": _float("additional_consumptions.interior_climatization_consumption"),
            "residual_consumption":      _float("additional_consumptions.residual_consumption"),
            "ascent_consumption":        _float("slope_consumption_values.ascent_slope_consumption.physical_value"),
            # Status (Text)
            "charge_state":             fields.get("charging_state_report.current_charge_state"),
            "charge_type":              fields.get("charging_state_report.charge_type"),
            "charge_mode":              fields.get("charging_state_report.charge_mode"),
            "update_reason":            fields.get("update_reason"),
            # Binary
            "locked":                   fields.get("locked") == "true" if fields.get("locked") is not None else None,
            # Zeitstempel
            "car_captured_at":          fields.get("car_captured_utc_timestamp"),
        })

        return result
