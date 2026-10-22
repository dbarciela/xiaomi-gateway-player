from __future__ import annotations

import importlib
import sys

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

DOMAIN = "xiaomi_gateway_player"
PLATFORMS = [Platform.MEDIA_PLAYER]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Xiaomi Gateway Media & TTS Player from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    mod_name = f"custom_components.{DOMAIN}.media_player"
    if mod_name in sys.modules:
        await hass.async_add_executor_job(importlib.reload, sys.modules[mod_name])
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
