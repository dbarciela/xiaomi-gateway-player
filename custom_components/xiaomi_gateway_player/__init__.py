from __future__ import annotations

import importlib
import sys

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

DOMAIN = "xiaomi_gateway_player"
PLATFORMS = [Platform.MEDIA_PLAYER]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Xiaomi Gateway Media & TTS Player from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    for submod in ("config_flow", "media_player"):
        mod_name = f"custom_components.{DOMAIN}.{submod}"
        if mod_name in sys.modules:
            await hass.async_add_executor_job(importlib.reload, sys.modules[mod_name])
        else:
            await hass.async_add_executor_job(importlib.import_module, mod_name)

    cf_mod = sys.modules.get(f"custom_components.{DOMAIN}.config_flow")
    if cf_mod and hasattr(cf_mod, "XiaomiGatewayPlayerConfigFlow"):
        config_entries.HANDLERS[DOMAIN] = cf_mod.XiaomiGatewayPlayerConfigFlow
    entry.__dict__.pop("supports_options", None)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
