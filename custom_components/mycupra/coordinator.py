"""DataUpdateCoordinator für die MyCupra (Read-Only) Integration.

Wichtig: cupra_client.CupraClient ist bewusst synchron (blockierende Netzwerk-
aufrufe, blockierendes time.sleep() in der Retry-Logik). Home Assistant läuft
in einer asyncio-Event-Loop, in der blockierender Code NIEMALS direkt aufgerufen
werden darf, da das den gesamten HA-Kern einfrieren würde.

Die Lösung: hass.async_add_executor_job() führt den synchronen Code in einem
separaten Thread aus, die Event-Loop bleibt frei. Das ist insbesondere wichtig,
weil CupraClient.list_files()/download_latest() bei anhaltenden Netzwerk-
problemen über die gestaffelte Backoff-Strategie (siehe cupra_client.py)
theoretisch stundenlang blockieren können (10s -> 1min -> 10min -> 20min ->
stündlich, siehe RETRY_SCHEDULE) - das darf niemals im HA-Hauptthread laufen.
"""

from __future__ import annotations

import logging
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
    """Holt periodisch die neueste Datendatei vom EU Data Act Portal und
    stellt das geparste Ergebnis den Sensor-Entities zur Verfügung."""

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
        """Wird von DataUpdateCoordinator in jedem Update-Intervall aufgerufen.

        Läuft im Executor-Thread (siehe Modul-Docstring), damit die ggf. lange
        blockierende Retry-Logik des CupraClient die HA-Event-Loop nicht
        einfriert. Gibt das geparste JSON der neuesten Datendatei zurück.

        ACHTUNG zur Retry-Architektur: CupraClient versucht bei vorübergehenden
        Fehlern (Netzwerk, abgelehnter Token) bereits selbst unbegrenzt mit
        gestaffelten Wartezeiten erneut (siehe RETRY_SCHEDULE in cupra_client.py).
        Das heißt, dieser Coordinator-Aufruf kann im Fehlerfall sehr lange dauern
        (Stunden), bevor er überhaupt zurückkehrt - das ist beabsichtigt und vom
        Nutzer so gewünscht (dauerhafte automatische Wiederherstellung), läuft
        aber dank async_add_executor_job in einem eigenen Thread, ohne HA zu
        blockieren. CupraPermanentError (falsches Passwort, falsche VIN) wird
        NICHT wiederholt und kommt sofort zurück - hier wird die Entity in HA
        auf 'nicht verfügbar' gesetzt und ein Repair-Hinweis wäre über
        UpdateFailed sichtbar."""
        try:
            raw_bytes, filename = await self.hass.async_add_executor_job(
                self.client.download_latest
            )
        except CupraPermanentError as err:
            # Dauerhafter Fehler (falsches Passwort, falsche VIN/Identifier) -
            # Nutzer muss die Integration neu konfigurieren (Re-Auth-Flow).
            raise UpdateFailed(
                f"Dauerhafter Fehler, Konfiguration prüfen: {err}"
            ) from err
        except CupraLoginError as err:
            # Sollte praktisch nicht auftreten, da CupraClient intern schon
            # unbegrenzt retry-t - zur Sicherheit trotzdem abgefangen.
            raise UpdateFailed(f"Datenabruf fehlgeschlagen: {err}") from err

        parsed = await self.hass.async_add_executor_job(
            self._parse_zip, raw_bytes, filename
        )
        return parsed

    @staticmethod
    def _parse_zip(raw_bytes: bytes, filename: str) -> dict:
        """Platzhalter für die ZIP/JSON-Auswertung.

        Wird im nächsten Schritt durch die separat entwickelte und getestete
        Parsing-Logik ersetzt (siehe geplantes eigenständiges Auswertungs-Skript).
        Gibt vorerst nur Metadaten zurück, damit der Coordinator schon jetzt
        lauffähig ist und getestet werden kann."""
        return {
            "_raw_filename": filename,
            "_raw_size_bytes": len(raw_bytes),
        }
