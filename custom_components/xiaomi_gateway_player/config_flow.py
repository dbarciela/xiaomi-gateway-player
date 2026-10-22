from __future__ import annotations

from typing import Any
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.network import NoURLAvailableError, get_url

DOMAIN = "xiaomi_gateway_player"


class XiaomiGatewayPlayerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Xiaomi Gateway Media & TTS Player."""

    VERSION = 1

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
            default_ha_url = get_url(self.hass, allow_internal=True, allow_ip=True, prefer_external=False)
        except NoURLAvailableError:
            default_ha_url = "http://192.168.1.150:8123"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("host"): str,
                    vol.Required("token"): str,
                    vol.Required("ha_url", default=default_ha_url): str,
                    vol.Optional("name", default="Coluna Gateway Hall"): str,
                }
            ),
            errors=errors,
        )
