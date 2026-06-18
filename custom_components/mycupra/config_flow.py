"""Config Flow für die MyCupra (Read-Only) Integration.

Mehrstufiger Flow:
  Schritt 1 (user):      E-Mail + Passwort eingeben -> Login validieren
  Schritt 2 (vin):       VIN aus dem Portal auswählen (Dropdown)
  Schritt 3 (settings):  Gerätename + Update-Intervall, Identifier wird
                         automatisch aus dem Portal ausgelesen
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
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
)
from .cupra_client import CupraClient, CupraLoginError, CupraPermanentError

_LOGGER = logging.getLogger(__name__)


class CannotConnect(HomeAssistantError):
    """Vorübergehender Fehler beim Verbindungsversuch (Netzwerk, Server)."""


class InvalidAuth(HomeAssistantError):
    """Dauerhafter Fehler: falsches Passwort oder falsche E-Mail."""


class NoVehicles(HomeAssistantError):
    """Keine Fahrzeuge im Portal für diesen Account gefunden."""


class NoDataRequest(HomeAssistantError):
    """Keine Daueranfrage (type=partial) im Portal für diese VIN angelegt."""


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Mehrstufiger Config Flow für MyCupra (Read-Only)."""

    VERSION = 1

    def __init__(self) -> None:
        self._email: str = ""
        self._password: str = ""
        self._client: CupraClient | None = None
        self._available_vins: list[str] = []

    # ------------------------------------------------------------------
    # Schritt 1: E-Mail + Passwort
    # ------------------------------------------------------------------
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            self._email = user_input["email"]
            self._password = user_input["password"]

            # Login + VINs abrufen (synchroner Client im Executor)
            try:
                vins = await self.hass.async_add_executor_job(
                    self._login_and_fetch_vins
                )
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except NoVehicles:
                errors["base"] = "no_vehicles"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unerwarteter Fehler beim Login")
                errors["base"] = "unknown"
            else:
                self._available_vins = vins
                return await self.async_step_vin()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("email"): str,
                vol.Required("password"): str,
            }),
            errors=errors,
        )

    def _login_and_fetch_vins(self) -> list[str]:
        """Synchron: Login + VIN-Liste aus dem Portal abrufen."""
        # Temporärer Client ohne VIN/Identifier nur für Login + VIN-Abfrage
        client = CupraClient(email=self._email, password=self._password, vin="")
        try:
            client.login()
        except CupraPermanentError as err:
            raise InvalidAuth from err
        except CupraLoginError as err:
            raise CannotConnect from err

        try:
            vins = client.fetch_vins()
        except CupraLoginError as err:
            raise CannotConnect from err

        if not vins:
            raise NoVehicles

        self._client = client
        return vins

    # ------------------------------------------------------------------
    # Schritt 2: VIN auswählen
    # ------------------------------------------------------------------
    async def async_step_vin(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            selected_vin = user_input[CONF_VIN]

            # Unique-ID-Check: VIN darf nicht bereits konfiguriert sein
            await self.async_set_unique_id(selected_vin)
            self._abort_if_unique_id_configured()

            # Identifier automatisch aus dem Portal auslesen
            try:
                identifier = await self.hass.async_add_executor_job(
                    self._fetch_identifier_for_vin, selected_vin
                )
            except NoDataRequest:
                errors["base"] = "no_data_request"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Fehler beim Auslesen des Identifiers")
                errors["base"] = "unknown"
            else:
                return await self.async_step_settings(
                    prefill={CONF_VIN: selected_vin, CONF_REQUEST_IDENTIFIER: identifier}
                )

        # Dropdown-Auswahl: bei einer VIN direkt vorausfüllen
        if len(self._available_vins) == 1:
            default_vin = self._available_vins[0]
        else:
            default_vin = vol.UNDEFINED

        return self.async_show_form(
            step_id="vin",
            data_schema=vol.Schema({
                vol.Required(CONF_VIN, default=default_vin): vol.In(self._available_vins),
            }),
            errors=errors,
        )

    def _fetch_identifier_for_vin(self, vin: str) -> str:
        """Synchron: Identifier der Daueranfrage für die gewählte VIN abrufen."""
        assert self._client is not None
        self._client.vin = vin
        self._client.request_identifier = ""
        try:
            return self._client.fetch_request_identifier()
        except CupraLoginError as err:
            raise NoDataRequest from err

    # ------------------------------------------------------------------
    # Schritt 3: Gerätename + Update-Intervall
    # ------------------------------------------------------------------
    async def async_step_settings(
        self,
        user_input: dict[str, Any] | None = None,
        prefill: dict[str, Any] | None = None,
    ) -> FlowResult:
        if prefill:
            # Erste Anzeige des Formulars mit vorausgefüllten Werten
            self._prefill = prefill
            return self.async_show_form(
                step_id="settings",
                data_schema=vol.Schema({
                    vol.Optional(CONF_DEVICE_NAME, default=DEFAULT_DEVICE_NAME): str,
                    vol.Optional(
                        CONF_UPDATE_INTERVAL_MINUTES,
                        default=DEFAULT_UPDATE_INTERVAL_MINUTES,
                    ): vol.All(int, vol.Range(min=5)),
                }),
                errors={},
            )

        if user_input is not None:
            data = {
                "email": self._email,
                "password": self._password,
                CONF_VIN: self._prefill[CONF_VIN],
                CONF_REQUEST_IDENTIFIER: self._prefill[CONF_REQUEST_IDENTIFIER],
                CONF_DEVICE_NAME: user_input[CONF_DEVICE_NAME],
                CONF_UPDATE_INTERVAL_MINUTES: user_input[CONF_UPDATE_INTERVAL_MINUTES],
            }
            return self.async_create_entry(
                title=user_input[CONF_DEVICE_NAME],
                data=data,
            )

        # Fallback falls direkt aufgerufen ohne prefill
        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema({
                vol.Optional(CONF_DEVICE_NAME, default=DEFAULT_DEVICE_NAME): str,
                vol.Optional(
                    CONF_UPDATE_INTERVAL_MINUTES,
                    default=DEFAULT_UPDATE_INTERVAL_MINUTES,
                ): vol.All(int, vol.Range(min=5)),
            }),
            errors={},
        )
