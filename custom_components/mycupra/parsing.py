"""Reine Auswertungs-Funktionen für die MyCupra-Daten (ohne Home-Assistant-Imports).

Getrennt vom Coordinator, damit die Logik ohne Home Assistant getestet werden kann.
"""

from __future__ import annotations

import io
import json
import logging
import zipfile
from datetime import datetime, timezone
from typing import Any

_LOGGER = logging.getLogger(__name__)

EMPTY_FILE_MARKER = "no_content_found"
# Eine leere ZIP ("no_content_found") ist 264 Bytes groß. Echte Datendateien sind
# im Bereich mehrerer KB. Die Größe dient nur als Zusatzprüfung.
EMPTY_FILE_MAX_BYTES = 300

# Version der Auswertung. Wird sie erhöht, werden die abgelegten Rohdateien einmal neu
# eingelesen (neue Felder bekommen dadurch sofort einen Wert).
PARSE_VERSION = 3

# Felder, die nach UUID-Key statt nach Namen gelesen werden, weil der Name allein nicht
# eindeutig ist. Quelle: Data Dictionary "Continuous Data" v1.0.6.
KEY_FIELDS: dict[str, str] = {
    # Name "value": "Value of the primary range" (Reichweite in km)
    "0ca40e18-0564-3eda-bcc0-7aee9ef44f04": "key:range_primary",
    # Ladezustand: drei Einträge gleichen Namens, nur dieser ändert sich mit der Ladung
    # (am 04./05.10.2026 gegen die go-e Wallbox verifiziert; die anderen beiden sind konstant).
    "a08cca2b-ed42-37bc-b160-d015ce205d3d": "key:charge_state",
    # Aktuelle Ladeleistung (kW) und Restladezeit; die übrigen Einträge gleichen Namens sind konstant.
    "c8cb205f-01c6-3c81-bda1-059b99ae6515": "key:charge_power",
    "7405c11f-4d20-36d2-8381-18364aa1f444": "key:remaining_time",
}

# Zeitstempel, aus denen der "Datenstand" (neueste Fahrzeugmeldung in der Datei) gebildet wird.
LATEST_SOURCES = (
    "car_captured_utc_timestamp",
    "car_captured_time",
    "profile_state_report.car_captured_time",
)
LATEST_FIELD = "_latest_capture"


def is_empty_file(entry: dict) -> bool:
    """True, wenn die Portal-Datei laut Name/Größe keine Daten enthält."""
    name = str(entry.get("name", ""))
    if EMPTY_FILE_MARKER in name:
        return True
    try:
        return int(entry.get("size")) <= EMPTY_FILE_MAX_BYTES
    except (TypeError, ValueError):
        return False


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def extract_fields(raw_bytes: bytes, filename: str = "?") -> dict[str, str] | None:
    """Liest eine Daten-ZIP und liefert {dataFieldName: value}.

    Mehrfach vorkommende Feldnamen: der erste Wert in der Datei gewinnt (wie bisher,
    am 11.07.2026 gegen die Cupra-App verifiziert). Zusätzlich:
    - Felder aus KEY_FIELDS stehen unter ihrem Key-Pseudonamen ("key:...").
    - LATEST_FIELD enthält den neuesten Fahrzeug-Zeitstempel der ganzen Datei.
    Gibt None zurück, wenn die Datei nicht lesbar ist.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
            json_name = next((n for n in zf.namelist() if n.endswith(".json")), None)
            if not json_name:
                _LOGGER.warning("Keine JSON-Datei in ZIP %s gefunden.", filename)
                return None
            data = json.loads(zf.read(json_name))
    except Exception as err:  # noqa: BLE001
        _LOGGER.error("Fehler beim Entpacken/Parsen von %s: %s", filename, err)
        return None

    fields: dict[str, str] = {}
    latest: datetime | None = None
    for entry in data.get("Data", []):
        name = entry.get("dataFieldName", "")
        value = entry.get("value", "")
        key = entry.get("key", "")
        if key in KEY_FIELDS:
            fields[KEY_FIELDS[key]] = value
        if name and name not in fields:
            fields[name] = value
        if name in LATEST_SOURCES:
            parsed = _parse_timestamp(value)
            if parsed and (latest is None or parsed > latest):
                latest = parsed
    if latest is not None:
        fields[LATEST_FIELD] = latest.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return fields


def merge_fields(
    store: dict[str, str],
    store_ts: dict[str, str],
    new_fields: dict[str, str],
    file_ts: str,
) -> int:
    """Übernimmt Felder einer Datei, aber nur wenn die Datei neuer (oder gleich alt)
    ist als die Datei, aus der der bisherige Wert stammt. Liefert die Zahl geänderter
    Felder. So gewinnt pro Feld immer der Wert aus der neuesten Datei, egal in welcher
    Reihenfolge die Dateien verarbeitet werden.

    Ausnahme LATEST_FIELD (Datenstand): hier gewinnt immer der größte Zeitstempel.
    """
    changed = 0
    for name, value in new_fields.items():
        if name == LATEST_FIELD:
            old = _parse_timestamp(store.get(name))
            new = _parse_timestamp(value)
            if new is not None and (old is None or new > old):
                store[name] = value
                store_ts[name] = file_ts
                changed += 1
            continue
        if file_ts >= store_ts.get(name, ""):
            if store.get(name) != value:
                changed += 1
            store[name] = value
            store_ts[name] = file_ts
    return changed


_SETTING_TEXT = {
    "immediatecharging": "sofort laden",
    "maximum": "maximal",
    "reduced": "reduziert",
    "permanent": "dauerhaft",
    "off": "aus",
}


def _setting_text(value: str | None, prefix: str) -> str | None:
    """'MAX_CHARGE_CURRENT_AC_MAXIMUM' -> 'maximal' (Präfix entfernen, übersetzen)."""
    if not value:
        return None
    text = str(value)
    if text.startswith(prefix):
        text = text[len(prefix):]
    text = text.replace("_", " ").lower()
    return _SETTING_TEXT.get(text, text)


def build_result(fields: dict[str, str]) -> dict[str, Any]:
    """Wandelt die zusammengeführten Rohfelder in die Sensorwerte um."""

    def _float(key: str):
        v = fields.get(key)
        try:
            return float(v) if v is not None else None
        except (ValueError, TypeError):
            return None

    def _float_positive(key: str):
        """None wenn Wert <= 0 (VW Sentinel für 'nicht anwendbar')."""
        v = _float(key)
        return v if (v is not None and v > 0) else None

    def _int(key: str):
        v = fields.get(key)
        try:
            return int(float(v)) if v is not None else None
        except (ValueError, TypeError):
            return None

    def _int_positive(key: str):
        v = _int(key)
        return v if (v is not None and v > 0) else None

    def _seconds_to_minutes_positive(key: str):
        """'33000s' -> 550 Minuten. None wenn <= 0 (Sentinel)."""
        v = fields.get(key)
        if v is None:
            return None
        try:
            minutes = round(int(str(v).rstrip("s")) / 60)
            return minutes if minutes > 0 else None
        except (ValueError, TypeError):
            return None

    def _soc_from_energy() -> int | None:
        current = _float("energy_contents.current_energy_content.physical_value")
        maximum = _float("energy_contents.maximal_energy_content.physical_value")
        if current is not None and maximum and maximum > 0:
            return round(current / maximum * 100)
        return None

    # SOC: primär battery_level_HV.value (entspricht der App, verifiziert 11.07.2026),
    # Fallback 1: battery_state_report.soc (nur beim/nach Laden), Fallback 2: Energie.
    soc = (
        _int("battery_level_HV.value")
        or _int("battery_state_report.soc")
        or _soc_from_energy()
    )

    # energy_contents.*.physical_value ist in Zehntel-kWh (773.5 == 77,35 kWh).
    current_energy = _float("energy_contents.current_energy_content.physical_value")
    max_energy = _float("energy_contents.maximal_energy_content.physical_value")

    locked_raw = fields.get("locked")

    # Datenstand: neuester Fahrzeug-Zeitstempel aller Dateien; Fallback auf das alte Feld.
    captured = _parse_timestamp(fields.get(LATEST_FIELD)) or _parse_timestamp(
        fields.get("car_captured_utc_timestamp")
    )

    # Ladezustand key-genau; Fallback auf den Namen, falls der Key (noch) nicht vorkam.
    charge_state = fields.get("key:charge_state") or fields.get(
        "charging_state_report.current_charge_state"
    )
    charging = charge_state == "CHARGE_STATE_CHARGING_HV_BATTERY"
    # Restladezeit nur während des Ladens (am Ladeende meldet das Fahrzeug 60 s).
    remaining = _seconds_to_minutes_positive("key:remaining_time") if charging else None
    if "key:remaining_time" not in fields:
        remaining = _seconds_to_minutes_positive("battery_state_report.remaining_charging_time_complete")
    power = _float("key:charge_power")
    if power is None:
        power = _float("battery_state_report.charge_power")

    return {
        # Batterie
        "soc": soc,
        "current_energy_kwh": round(current_energy / 10, 2) if current_energy is not None else None,
        "max_energy_kwh": round(max_energy / 10, 2) if max_energy is not None else None,
        "range_km": _int_positive("key:range_primary"),
        "charge_power_kw": power,
        "charge_rate_km_h": _float_positive("battery_state_report.charge_rate"),
        "remaining_charge_min": remaining,
        # Energie des letzten Ladeblocks (Wallbox 04./05.10.2026: 0,5/2,5/4,5 kWh gegen 0,57/2,56/4,63 kWh;
        # die App zeigt dafür gerundete Werte, z. B. 18,0 gegen "~19 kWh")
        "last_charge_energy_kwh": _float_positive("battery_state_report.charge_energy"),
        "target_soc": _int("settings.target_soc"),
        "battery_care_limit": _int("battery_care_mode.charge_bcam_threshold"),
        # Fahrzeug
        "mileage_km": _int("mileage.value"),
        "outdoor_temperature": _float("outdoor_temperature"),
        "min_temperature": _float("min_temperature"),
        "max_temperature": _float("max_temperature"),
        # Verbrauch
        "climatization_consumption": _float("additional_consumptions.interior_climatization_consumption"),
        "residual_consumption": _float("additional_consumptions.residual_consumption"),
        "ascent_consumption": _float("slope_consumption_values.ascent_slope_consumption.physical_value"),
        "descent_consumption": _float("slope_consumption_values.descent_slope_consumption.physical_value"),
        # Status (Text)
        "charge_state": charge_state,
        "charge_type": fields.get("charging_state_report.charge_type"),
        "charge_mode": fields.get("charging_state_report.charge_mode"),
        "update_reason": fields.get("update_reason"),
        # Einstellungen (Text)
        "max_charge_current_ac": _setting_text(fields.get("settings.max_charge_current_ac"), "MAX_CHARGE_CURRENT_AC_"),
        "auto_unlock_ac": _setting_text(fields.get("settings.auto_unlock_ac"), "AUTO_UNLOCK_AC_"),
        "charge_mode_selection": _setting_text(fields.get("settings.charge_mode_selection"), "CHARGE_MODE_SELECTION_"),
        # Binary
        "locked": (locked_raw == "true") if locked_raw is not None else None,
        # Zeitstempel (datetime, weil der Sensor device_class TIMESTAMP hat)
        "car_captured_at": captured,
    }
