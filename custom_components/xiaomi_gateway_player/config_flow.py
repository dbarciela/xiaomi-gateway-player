from __future__ import annotations

from typing import Any
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.network import NoURLAvailableError, get_url

DOMAIN = "xiaomi_gateway_player"

DEFAULT_FAVORITES_BY_COUNTRY = {
    "PT": "Antena 1, TSF, Rádio Nova Era, Smooth FM, Rádio Comercial, RFM",
}


class XiaomiGatewayPlayerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Xiaomi Gateway Media & TTS Player."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> XiaomiGatewayPlayerOptionsFlow:
        """Get the options flow for this handler."""
        return XiaomiGatewayPlayerOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            token = user_input["token"].strip()
            if len(token) != 32:
                errors["token"] = "invalid_token"
            else:
                await self.async_set_unique_id(user_input["host"].strip())
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input.get("name", "Coluna Gateway Hall"),
                    data=user_input,
                )

        try:
            default_ha_url = get_url(
                self.hass, allow_internal=True, allow_ip=True, prefer_external=False
            )
        except NoURLAvailableError:
            default_ha_url = "http://192.168.1.150:8123"

        country = (self.hass.config.country or "").upper()
        default_favs = DEFAULT_FAVORITES_BY_COUNTRY.get(country, "")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("host"): str,
                    vol.Required("token"): str,
                    vol.Required("ha_url", default=default_ha_url): str,
                    vol.Optional("name", default="Coluna Gateway Hall"): str,
                    vol.Optional("radio_country", default=country): str,
                    vol.Optional("favorite_radios", default=default_favs): str,
                }
            ),
            errors=errors,
        )


class XiaomiGatewayPlayerOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for Xiaomi Gateway Media & TTS Player (Radio Browser & Subsonic)."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        country_default = (self.hass.config.country or "").upper()
        current_country = self._config_entry.options.get(
            "radio_country",
            self._config_entry.data.get("radio_country", country_default),
        )
        default_favs = DEFAULT_FAVORITES_BY_COUNTRY.get(current_country.upper(), "")
        current_favs = self._config_entry.options.get(
            "favorite_radios",
            self._config_entry.data.get("favorite_radios", default_favs),
        )
        current_ha_url = self._config_entry.options.get(
            "ha_url",
            self._config_entry.data.get("ha_url", "http://192.168.1.150:8123"),
        )
        subsonic_url = self._config_entry.options.get("subsonic_url", "")
        subsonic_user = self._config_entry.options.get("subsonic_user", "")
        subsonic_password = self._config_entry.options.get("subsonic_password", "")

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional("radio_country", default=current_country): str,
                    vol.Optional("favorite_radios", default=current_favs): str,
                    vol.Optional("ha_url", default=current_ha_url): str,
                    vol.Optional("subsonic_url", default=subsonic_url): str,
                    vol.Optional("subsonic_user", default=subsonic_user): str,
                    vol.Optional("subsonic_password", default=subsonic_password): str,
                }
            ),
        )
