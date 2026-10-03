"""DataUpdateCoordinator für die MyCupra (Read-Only) Integration.

Ablauf pro Aktualisierung (alle 15 min):
1. Dateiliste vom Portal holen (das Portal hält nur die letzten ~30 Dateien vor).
2. Alle noch nicht verarbeiteten Dateien mit Inhalt herunterladen und auswerten.
   Leere Dateien ("no_content_found") werden übersprungen.
3. Pro Feld gewinnt der Wert aus der NEUESTEN Datei, in der das Feld vorkommt.
4. Der Stand (Feldwerte + verarbeitete Dateien) wird in .storage gesichert, damit er
   einen HA-Neustart und das Herausfallen alter Dateien aus der Portal-Liste übersteht.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_DEVICE_NAME,
    CONF_REQUEST_IDENTIFIER,
    CONF_UPDATE_INTERVAL_MINUTES,
    CONF_VIN,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
)
from .cupra_client import (
    CupraActionRequired,
    CupraClient,
    CupraLoginError,
    CupraPermanentError,
    authorize_url,
)
from .parsing import build_result, extract_fields, is_empty_file, merge_fields

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1

# Werte, deren Änderung als INFO ins Log geschrieben wird.
_LOGGED_KEYS = ("soc", "mileage_km", "charge_state", "locked", "car_captured_at")


class MyCupraCoordinator(DataUpdateCoordinator[dict]):
    """Holt periodisch neue Datendateien vom EU Data Act Portal und führt sie zusammen."""

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

        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}_{self.vin}_state")
        # Feldname -> Wert / Zeitstempel (createdOn) der Datei, aus der der Wert stammt
        self._fields: dict[str, str] = {}
        self._field_ts: dict[str, str] = {}
        # Bereits verarbeitete Dateien: Dateiname -> createdOn
        self._seen: dict[str, str] = {}
        self._last_data_file: str | None = None
        self._last_data_file_ts: str | None = None
        self._last_data_file_size: int | None = None
        # Meldung "Bestätigung bei VW nötig" (persistente Benachrichtigung in HA)
        self._notification_id = f"{DOMAIN}_{self.vin}_action_required"
        self._action_required_notified = False

    def _notify_action_required(self, err: CupraActionRequired) -> None:
        """VW verlangt eine Bestätigung im Browser: Fehler melden, nichts automatisch bestätigen."""
        _LOGGER.error(
            "VW verlangt eine Bestätigung im Browser (Login-Schritt %s, Seite %s, Titel %r). "
            "Das Add-on bestätigt nichts automatisch. Link und Anleitung stehen in der "
            "Home-Assistant-Benachrichtigung.",
            err.step, err.page, err.title,
        )
        if self._action_required_notified:
            return
        interval = int(self.update_interval.total_seconds() // 60) if self.update_interval else 15
        message = (
            "VW zeigt beim Login des Add-ons eine Seite, die nur du bestätigen kannst "
            "(vermutlich geänderte Nutzungs- oder Datenschutzbedingungen). "
            "Das Add-on bestätigt nichts automatisch und holt bis dahin keine neuen Daten.\n\n"
            "**So geht es weiter**\n"
            f"1. Diesen Link im Browser öffnen (PC oder Handy): [VW-Anmeldung öffnen]({authorize_url()})\n"
            "2. Mit dem VW-/Cupra-Konto anmelden.\n"
            "3. Die angezeigte Seite lesen und selbst bestätigen.\n"
            f"4. Danach läuft die Integration beim nächsten Abruf (alle {interval} Minuten) "
            "von selbst weiter, diese Meldung verschwindet dann.\n\n"
            "Hilfe in Claude: einfach diese Meldung nennen, die Anleitung steht im Second Brain "
            "(konzepte/vw-zustimmung).\n\n"
            f"Technische Angaben: Login-Schritt {err.step}, Seite `{err.page}`, Titel: {err.title!r}."
        )
        persistent_notification.async_create(
            self.hass,
            message,
            title="MyCupra: Bestätigung bei VW nötig",
            notification_id=self._notification_id,
        )
        self._action_required_notified = True

    def _clear_action_required(self) -> None:
        if self._action_required_notified:
            persistent_notification.async_dismiss(self.hass, self._notification_id)
            self._action_required_notified = False
            _LOGGER.info("Login wieder möglich - Meldung 'Bestätigung bei VW nötig' entfernt.")

    async def _async_setup(self) -> None:
        """Gespeicherten Stand laden (wird vor dem ersten Abruf aufgerufen)."""
        stored = await self._store.async_load()
        if not stored:
            _LOGGER.info("Kein gespeicherter Stand vorhanden - starte mit leerem Zwischenspeicher.")
            return
        self._fields = dict(stored.get("fields", {}))
        self._field_ts = dict(stored.get("field_ts", {}))
        self._seen = dict(stored.get("seen", {}))
        self._last_data_file = stored.get("last_data_file")
        self._last_data_file_ts = stored.get("last_data_file_ts")
        self._last_data_file_size = stored.get("last_data_file_size")
        _LOGGER.info(
            "Gespeicherter Stand geladen: %d Felder, %d bekannte Dateien, letzte Datendatei %s",
            len(self._fields), len(self._seen), self._last_data_file,
        )

    async def _async_save(self) -> None:
        await self._store.async_save(
            {
                "fields": self._fields,
                "field_ts": self._field_ts,
                "seen": self._seen,
                "last_data_file": self._last_data_file,
                "last_data_file_ts": self._last_data_file_ts,
                "last_data_file_size": self._last_data_file_size,
            }
        )

    async def _async_update_data(self) -> dict:
        _LOGGER.debug("Aktualisierung gestartet")
        try:
            files = await self.hass.async_add_executor_job(self.client.list_files)
        except CupraActionRequired as err:
            self._notify_action_required(err)
            raise UpdateFailed(
                "Bestätigung bei VW nötig - siehe Benachrichtigung in Home Assistant"
            ) from err
        except CupraPermanentError as err:
            _LOGGER.error("Dauerhafter Fehler bei der Dateiliste: %s", err)
            raise UpdateFailed(f"Dauerhafter Fehler, Konfiguration prüfen: {err}") from err
        except CupraLoginError as err:
            _LOGGER.warning("Dateiliste nicht abrufbar: %s", err)
            raise UpdateFailed(f"Datenabruf fehlgeschlagen: {err}") from err

        self._clear_action_required()
        files = sorted(files, key=lambda f: f.get("createdOn", ""))
        empty = [f for f in files if is_empty_file(f)]
        with_data = [f for f in files if not is_empty_file(f)]
        new_files = [f for f in files if f["name"] not in self._seen]
        new_data = [f for f in new_files if not is_empty_file(f)]
        _LOGGER.info(
            "Dateiliste: %d Dateien (%d mit Daten, %d leer), %d neu, davon %d mit Daten. "
            "Neueste Datei: %s",
            len(files), len(with_data), len(empty), len(new_files), len(new_data),
            files[-1]["name"] if files else "-",
        )
        if not files:
            _LOGGER.warning("Das Portal liefert keine Dateien - Daueranfrage im Portal prüfen.")

        previous = dict(self.data) if self.data else build_result(self._fields)
        state_changed = False

        # Älteste zuerst; merge_fields sorgt dafür, dass pro Feld immer die neueste Datei gewinnt.
        for entry in new_data:
            name = entry["name"]
            created = entry.get("createdOn", "")
            try:
                raw = await self.hass.async_add_executor_job(self.client.download_file, name)
            except CupraActionRequired as err:
                self._notify_action_required(err)
                raise UpdateFailed(
                    "Bestätigung bei VW nötig - siehe Benachrichtigung in Home Assistant"
                ) from err
            except CupraPermanentError as err:
                _LOGGER.error("Dauerhafter Fehler beim Download von %s: %s", name, err)
                raise UpdateFailed(f"Dauerhafter Fehler, Konfiguration prüfen: {err}") from err
            except CupraLoginError as err:
                # Nicht als gesehen markieren, damit der nächste Lauf es erneut versucht.
                _LOGGER.warning("Download von %s fehlgeschlagen, nächster Lauf versucht es erneut: %s", name, err)
                continue

            fields = await self.hass.async_add_executor_job(extract_fields, raw, name)
            if fields is None:
                _LOGGER.error("Datei %s nicht auswertbar - wird übersprungen und nicht erneut versucht.", name)
                self._seen[name] = created
                state_changed = True
                continue

            changed = merge_fields(self._fields, self._field_ts, fields, created)
            self._seen[name] = created
            if not self._last_data_file_ts or created >= self._last_data_file_ts:
                self._last_data_file = name
                self._last_data_file_ts = created
                self._last_data_file_size = len(raw)
            state_changed = True
            _LOGGER.info(
                "Datei %s (%s, %d Bytes) ausgewertet: %d Felder, %d davon neu oder geändert.",
                name, created, len(raw), len(fields), changed,
            )

        # Leere Dateien nur als gesehen vermerken.
        for entry in new_files:
            if is_empty_file(entry) and entry["name"] not in self._seen:
                self._seen[entry["name"]] = entry.get("createdOn", "")
                state_changed = True

        # Liste der gesehenen Dateien auf die aktuell im Portal vorhandenen begrenzen.
        current_names = {f["name"] for f in files}
        pruned = {n: c for n, c in self._seen.items() if n in current_names}
        if len(pruned) != len(self._seen):
            _LOGGER.debug("Gesehene Dateien bereinigt: %d -> %d", len(self._seen), len(pruned))
            self._seen = pruned
            state_changed = True

        if state_changed:
            await self._async_save()

        if not new_data:
            if self._fields:
                _LOGGER.info(
                    "Keine neuen Daten im Portal - behalte letzten Stand (letzte Datendatei: %s).",
                    self._last_data_file,
                )
            else:
                _LOGGER.warning(
                    "Keine Daten vorhanden: weder gespeicherter Stand noch Datendatei in der Portal-Liste. "
                    "Sensoren bleiben leer, bis das Fahrzeug Daten sendet."
                )

        result: dict[str, Any] = build_result(self._fields)
        result["_raw_filename"] = self._last_data_file
        result["_raw_size_bytes"] = self._last_data_file_size

        for key in _LOGGED_KEYS:
            if previous.get(key) != result.get(key):
                _LOGGER.info("Wert geändert: %s: %s -> %s", key, previous.get(key), result.get(key))

        _LOGGER.debug(
            "Aktualisierung beendet: %d Felder gespeichert, soc=%s, mileage_km=%s",
            len(self._fields), result.get("soc"), result.get("mileage_km"),
        )
        return result
