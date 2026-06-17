"""Config Flow für die MyCupra (Read-Only) Integration.

Validiert beim Einrichten die Zugangsdaten durch einen echten Login-Versuch,
damit der Nutzer bei Tippfehlern (Passwort, VIN) sofort eine klare Fehler-
meldung im Dialog sieht, statt eine kaputt konfigurierte Integration anzulegen,
die erst beim ersten Update-Intervall fehlschlägt.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError

from .const import (
    CONF_DEVICE_NAME,
    CONF_REQUEST_IDENTIFIER,
    CONF_UPDATE_INTERVAL_MINUTES,
    CONF_VIN,
    DEFAULT_DEVICE_NAME,
    DEFAULT_REQUEST_IDENTIFIER,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
)
from .cupra_client import CupraClient, CupraLoginError, CupraPermanentError

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("email"): str,
        vol.Required("password"): str,
        vol.Required(CONF_VIN): str,
        vol.Optional(CONF_DEVICE_NAME, default=DEFAULT_DEVICE_NAME): str,
        vol.Optional(
            CONF_REQUEST_IDENTIFIER, default=DEFAULT_REQUEST_IDENTIFIER
        ): str,
        vol.Optional(
            CONF_UPDATE_INTERVAL_MINUTES, default=DEFAULT_UPDATE_INTERVAL_MINUTES
        ): vol.All(int, vol.Range(min=5)),
    }
)


class CannotConnect(HomeAssistantError):
    """Vorübergehender Fehler beim Verbindungsversuch (Netzwerk, Server)."""


class InvalidAuth(HomeAssistantError):
    """Dauerhafter Fehler: falsches Passwort, falsche VIN oder Identifier."""


async def _validate_input(hass: HomeAssistant, data: dict[str, Any]) -> None:
    """Versucht einen echten Login + Dateiliste abzurufen, um die eingegebenen
    Daten zu validieren, bevor der Config-Entry angelegt wird.

    Läuft im Executor-Thread, da CupraClient synchron ist (siehe coordinator.py
    für die ausführliche Begründung). Nutzt validate_credentials() statt
    list_files()/download_latest(), da diese die unbegrenzte Retry-Logik des
    Clients nutzen würden - der Einrichtungsdialog soll bei einem Fehler sofort
    antworten, statt ggf. stundenlang zu hängen."""
    client = CupraClient(
        email=data["email"],
        password=data["password"],
        vin=data[CONF_VIN],
        request_identifier=data[CONF_REQUEST_IDENTIFIER],
    )

    def _try_once():
        client.validate_credentials()

    try:
        await hass.async_add_executor_job(_try_once)
    except CupraPermanentError as err:
        _LOGGER.debug("Validierung fehlgeschlagen (dauerhafter Fehler): %s", err)
        raise InvalidAuth from err
    except CupraLoginError as err:
        _LOGGER.debug("Validierung fehlgeschlagen (vorübergehender Fehler): %s", err)
        raise CannotConnect from err


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config Flow für MyCupra (Read-Only)."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Erster (und einziger) Schritt: alle Felder auf einmal abfragen."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                await _validate_input(self.hass, user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unerwarteter Fehler bei der Validierung")
                errors["base"] = "unknown"
            else:
                # VIN als eindeutige ID verwenden, damit dasselbe Fahrzeug
                # nicht zweimal eingerichtet werden kann.
                await self.async_set_unique_id(user_input[CONF_VIN])
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=user_input[CONF_DEVICE_NAME],
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )
