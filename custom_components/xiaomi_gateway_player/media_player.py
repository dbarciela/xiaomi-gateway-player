from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import socket
import struct
import threading
import time
from typing import Any
from urllib.parse import quote_plus, urlsplit

import aiohttp
import voluptuous as vol

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding as sym_padding

from homeassistant.components import media_source
from homeassistant.components.media_player import (
    BrowseMedia,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    async_process_play_media_url,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

_LOGGER = logging.getLogger(__name__)

DOMAIN = "xiaomi_gateway_player"
DYNAMIC_SLOT = "10030"
DYNAMIC_AAC_FILENAME = "gw_dynamic.aac"

_MIIO_LOCK = threading.Lock()
_MSG_ID_COUNTER = int(time.time() * 10) % 500000 + 60000


def _miio_send_command(
    host: str,
    token_hex: str,
    method: str,
    params: list[Any],
    timeout: float = 2.5,
) -> Any:
    """Send an encrypted Miio UDP packet directly to the Xiaomi Gateway with automatic retry."""
    global _MSG_ID_COUNTER
    token = bytes.fromhex(token_hex)
    key = hashlib.md5(token).digest()
    iv = hashlib.md5(key + token).digest()

    last_err: Exception | None = None
    for _attempt in range(2):
        with _MIIO_LOCK:
            _MSG_ID_COUNTER += 1
            msg_id = _MSG_ID_COUNTER

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        try:
            hello = bytes.fromhex("21310020ffffffffffffffffffffffffffffffffffffffffffffffffffffffff")
            sock.sendto(hello, (host, 54321))
            data, _ = sock.recvfrom(1024)
            dev_id = data[8:12]
            ts = struct.unpack(">I", data[12:16])[0]

            payload_dict = {"id": msg_id, "method": method, "params": params}
            payload_bytes = (json.dumps(payload_dict, separators=(",", ":")) + "\x00").encode("utf-8")

            padder = sym_padding.PKCS7(128).padder()
            padded = padder.update(payload_bytes) + padder.finalize()

            cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
            encryptor = cipher.encryptor()
            encrypted = encryptor.update(padded) + encryptor.finalize()

            length = 32 + len(encrypted)
            header = struct.pack(">HH", 0x2131, length) + b"\x00\x00\x00\x00" + dev_id + struct.pack(">I", ts + 1)
            checksum = hashlib.md5(header + token + encrypted).digest()
            packet = header + checksum + encrypted

            sock.sendto(packet, (host, 54321))
            resp, _ = sock.recvfrom(4096)

            enc_resp = resp[32:]
            if not enc_resp:
                return None
            dec_cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
            decryptor = dec_cipher.decryptor()
            decrypted_padded = decryptor.update(enc_resp) + decryptor.finalize()
            unpadder = sym_padding.PKCS7(128).unpadder()
            decrypted = unpadder.update(decrypted_padded) + unpadder.finalize()
            text = decrypted.decode("utf-8", errors="ignore").rstrip("\x00").strip()
            return json.loads(text)
        except Exception as err:
            last_err = err
        finally:
            sock.close()

    if last_err:
        raise last_err
    return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Xiaomi Gateway Media Player from a config entry."""
    host = entry.data["host"]
    token = entry.data["token"]
    ha_url = entry.data.get("ha_url", "http://192.168.1.150:8123").rstrip("/")
    name = entry.data.get("name", "Coluna Gateway Hall")

    player = XiaomiGatewayMediaPlayer(hass, host, token, ha_url, name, entry.entry_id)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = player
    async_add_entities([player], update_before_add=True)

    async def handle_save_sound(call: ServiceCall) -> None:
        url = call.data["url"]
        slot_id = str(call.data.get("slot_id", "10006"))
        volume = call.data.get("volume")
        play_now = call.data.get("play_now", True)
        await player.async_save_to_slot(url, slot_id, play_now=play_now, volume=volume)

    async def handle_delete_sound(call: ServiceCall) -> None:
        slot_id = str(call.data["slot_id"])
        await player.async_delete_slot(slot_id)

    if not hass.services.has_service(DOMAIN, "save_sound"):
        hass.services.async_register(
            DOMAIN,
            "save_sound",
            handle_save_sound,
            schema=vol.Schema(
                {
                    vol.Required("url"): cv.string,
                    vol.Optional("slot_id", default="10006"): vol.Coerce(str),
                    vol.Optional("play_now", default=True): cv.boolean,
                    vol.Optional("volume"): vol.All(vol.Coerce(int), vol.Range(min=1, max=100)),
                }
            ),
        )

    if not hass.services.has_service(DOMAIN, "delete_sound"):
        hass.services.async_register(
            DOMAIN,
            "delete_sound",
            handle_delete_sound,
            schema=vol.Schema(
                {
                    vol.Required("slot_id"): vol.Coerce(str),
                }
            ),
        )


class XiaomiGatewayMediaPlayer(MediaPlayerEntity):
    """Representation of the Xiaomi Mi Control Hub Speaker as a Home Assistant Media Player."""

    _attr_has_entity_name = False
    _attr_icon = "mdi:speaker-wireless"
    _attr_supported_features = (
        MediaPlayerEntityFeature.PLAY_MEDIA
        | MediaPlayerEntityFeature.BROWSE_MEDIA
        | MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_STEP
        | MediaPlayerEntityFeature.VOLUME_MUTE
        | MediaPlayerEntityFeature.STOP
        | MediaPlayerEntityFeature.TURN_OFF
    )

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        token: str,
        ha_url: str,
        name: str,
        entry_id: str,
    ) -> None:
        """Initialize the Xiaomi Gateway media player."""
        self.hass = hass
        self._host = host
        self._token = token
        self._ha_url = ha_url
        self._attr_name = name
        self.entity_id = "media_player.hall_gateway"
        self._attr_unique_id = "hall_gateway_media_player"
        self._attr_state = MediaPlayerState.IDLE
        self._attr_volume_level = 0.12  # Safe default (12%)
        self._attr_is_volume_muted = False
        self._attr_media_title: str | None = None
        self._attr_extra_state_attributes: dict[str, Any] = {
            "gateway_ip": host,
            "dynamic_slot": DYNAMIC_SLOT,
            "max_duration_seconds": 120,
            "max_slot_size_kb": 512,
            "stored_slots": [],
            "free_space_bytes": None,
            "last_transfer_ms": None,
        }
        self._lock = asyncio.Lock()

    @property
    def device_info(self) -> DeviceInfo:
        """Link to the existing Xiaomi Miio Gateway device if present."""
        dev_reg = dr.async_get(self.hass)
        for dev in dev_reg.devices.values():
            if dev.model and "lumi.gateway" in dev.model.lower():
                if dev.identifiers:
                    return DeviceInfo(identifiers=dev.identifiers)
        return DeviceInfo(
            identifiers={(DOMAIN, self._host)},
            name="Xiaomi Mi Control Hub",
            manufacturer="Xiaomi",
            model="DGNWG02LM (lumi.gateway.v3)",
        )

    async def _send_miio(self, method: str, params: list[Any], timeout: float = 2.5) -> Any:
        return await self.hass.async_add_executor_job(
            _miio_send_command, self._host, self._token, method, params, timeout
        )

    async def async_update(self) -> None:
        """Fetch stored custom slots and free flash space from the gateway."""
        try:
            info_resp = await self._send_miio("get_music_info", [0], timeout=2.5)
            if info_resp and "result" in info_resp:
                res = info_resp["result"]
                if isinstance(res, list) and res:
                    res = res[0]
                if isinstance(res, dict):
                    slots = [
                        {"slot_id": str(item.get("mid")), "duration_s": item.get("time")}
                        for item in res.get("list", [])
                        if int(item.get("mid", 0)) >= 10000
                    ]
                    self._attr_extra_state_attributes["stored_slots"] = slots

            space_resp = await self._send_miio("get_music_free_space", [], timeout=2.5)
            if space_resp and "result" in space_resp and space_resp["result"]:
                self._attr_extra_state_attributes["free_space_bytes"] = space_resp["result"][0]
        except Exception as err:
            _LOGGER.debug("Could not refresh Xiaomi Gateway music info: %s", err)

    async def async_set_volume_level(self, volume: float) -> None:
        """Set volume level, range 0..1."""
        self._attr_volume_level = max(0.0, min(1.0, volume))
        self.async_write_ha_state()

    async def async_mute_volume(self, mute: bool) -> None:
        """Mute or unmute volume."""
        self._attr_is_volume_muted = mute
        if mute:
            await self.async_media_stop()
        self.async_write_ha_state()

    async def async_media_stop(self) -> None:
        """Stop currently playing sound on the gateway."""
        try:
            await self._send_miio("set_sound_playing", ["off"])
        finally:
            self._attr_state = MediaPlayerState.IDLE
            self.async_write_ha_state()

    async def async_turn_off(self) -> None:
        """Turn off stops playback."""
        await self.async_media_stop()

    async def async_browse_media(
        self,
        media_content_type: MediaType | str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        """Implement the websocket media browsing helper (enables TTS & Local Media in UI)."""
        return await media_source.async_browse_media(
            self.hass,
            media_content_id,
            content_filter=lambda item: item.media_content_type.startswith("audio/"),
        )

    async def _resolve_source_url(self, media_id: str) -> str:
        """Resolve media-source://, MyInstants page URLs, MyInstants search queries, or local paths."""
        media_id = media_id.strip()
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

        # 1. Home Assistant media-source:// (TTS, Local Media, Radio Browser)
        if media_source.is_media_source_id(media_id):
            sourced_media = await media_source.async_resolve_media(
                self.hass, media_id, self.entity_id
            )
            raw_url = sourced_media.url
            if raw_url.startswith("/local/"):
                local_file = self.hass.config.path("www", raw_url[len("/local/"):])
                if os.path.exists(local_file):
                    return local_file
            processed = async_process_play_media_url(self.hass, raw_url)
            if raw_url.startswith("/"):
                parts = urlsplit(processed)
                pq = parts.path + (f"?{parts.query}" if parts.query else "")
                return f"http://127.0.0.1:8123{pq}"
            return processed

        # 2. MyInstants search query: "myinstants:vine boom"
        if media_id.lower().startswith("myinstants:"):
            query = media_id.split(":", 1)[1].strip()
            search_url = f"https://www.myinstants.com/pt/search/?name={quote_plus(query)}"
            session = async_get_clientsession(self.hass)
            async with session.get(
                search_url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                html = await resp.text()
            match = re.search(r"(/media/sounds/[^\"']+\.mp3)", html)
            if not match:
                raise ValueError(f"Nenhum som encontrado no MyInstants para: {query}")
            return f"https://www.myinstants.com{match.group(1)}"

        # 3. MyInstants page URL (e.g., https://www.myinstants.com/pt/instant/...)
        if "myinstants.com" in media_id and not media_id.lower().split("?")[0].endswith(".mp3"):
            session = async_get_clientsession(self.hass)
            async with session.get(
                media_id, headers=headers, timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                html = await resp.text()
            match = re.search(r"(/media/sounds/[^\"']+\.mp3)", html)
            if not match:
                raise ValueError(f"Não foi possível extrair o MP3 da página MyInstants: {media_id}")
            return f"https://www.myinstants.com{match.group(1)}"

        # 4. Relative /local/ path
        if media_id.startswith("/local/"):
            local_file = self.hass.config.path("www", media_id[len("/local/"):])
            if os.path.exists(local_file):
                return local_file
            return f"http://127.0.0.1:8123{media_id}"

        return media_id

    async def _convert_and_upload(self, source_url: str, slot_id: str, out_filename: str) -> float:
        """Convert audio to 22050Hz mono 32kbps ADTS AAC and upload to gateway flash slot."""
        t0 = time.monotonic()
        www_dir = self.hass.config.path("www")
        os.makedirs(www_dir, exist_ok=True)
        out_path = os.path.join(www_dir, out_filename)

        input_target = source_url
        if source_url.startswith(("http://", "https://")):
            tmp_input = f"/tmp/gw_in_{slot_id}.audio"
            session = async_get_clientsession(self.hass)
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            async with session.get(
                source_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                resp.raise_for_status()
                data = await resp.read()
            with open(tmp_input, "wb") as f:
                f.write(data)
            input_target = tmp_input

        # Convert with ffmpeg (limit to 110s so file stays < 500KB limit)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-i",
            input_target,
            "-t",
            "110",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "22050",
            "-c:a",
            "aac",
            "-b:a",
            "32k",
            "-f",
            "adts",
            out_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            err_msg = stderr.decode("utf-8", errors="ignore")[-300:]
            raise RuntimeError(f"Erro ffmpeg ao converter áudio: {err_msg}")

        # Tell gateway to download from Home Assistant HTTP /local/
        aac_url = f"{self._ha_url}/local/{out_filename}"
        await self._send_miio("download_user_music", [str(slot_id), aac_url], timeout=3.0)

        # Poll download progress rapidly (every 80ms, up to 4s)
        for _ in range(45):
            await asyncio.sleep(0.08)
            prog = await self._send_miio("get_download_progress", [], timeout=1.5)
            if prog and "result" in prog and prog["result"]:
                val = str(prog["result"][0])
                if val.endswith(":100") or val == "100":
                    break

        elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
        self._attr_extra_state_attributes["last_transfer_ms"] = elapsed_ms
        return elapsed_ms

    async def async_save_to_slot(
        self,
        url: str,
        slot_id: str,
        play_now: bool = True,
        volume: int | None = None,
    ) -> None:
        """Save any audio URL or MyInstants link permanently into a gateway slot (10001..10029)."""
        async with self._lock:
            resolved = await self._resolve_source_url(url)
            filename = f"gw_slot_{slot_id}.aac"
            await self._convert_and_upload(resolved, slot_id, filename)
            await self.async_update()
            if play_now:
                vol_int = volume if volume is not None else max(1, int(round(self._attr_volume_level * 100)))
                await self._send_miio("play_music_new", [str(slot_id), vol_int])
            self.async_write_ha_state()

    async def async_delete_slot(self, slot_id: str) -> None:
        """Delete a user music slot from gateway flash."""
        async with self._lock:
            await self._send_miio("delete_user_music", [str(slot_id)])
            await self.async_update()
            self.async_write_ha_state()

    async def async_play_media(
        self,
        media_type: MediaType | str,
        media_id: str,
        **kwargs: Any,
    ) -> None:
        """Play a piece of media (slot ID, TTS, MyInstants URL/query, or local/remote audio)."""
        async with self._lock:
            self._attr_state = MediaPlayerState.PLAYING
            self._attr_media_title = media_id
            self.async_write_ha_state()

            vol_int = 0 if self._attr_is_volume_muted else max(1, int(round(self._attr_volume_level * 100)))
            clean_id = media_id.strip()

            try:
                # Direct slot playback (e.g. "10005", "slot:10005", "ringtone:0") -> <50ms!
                for prefix in ("slot:", "ringtone:"):
                    if clean_id.lower().startswith(prefix):
                        clean_id = clean_id.split(":", 1)[1].strip()

                if clean_id.isdigit():
                    t0 = time.monotonic()
                    await self._send_miio("play_music_new", [str(clean_id), vol_int])
                    self._attr_extra_state_attributes["last_transfer_ms"] = round((time.monotonic() - t0) * 1000, 1)
                else:
                    resolved_url = await self._resolve_source_url(clean_id)
                    self._attr_extra_state_attributes["last_resolved_url"] = resolved_url
                    await self._convert_and_upload(resolved_url, DYNAMIC_SLOT, DYNAMIC_AAC_FILENAME)
                    await self._send_miio("play_music_new", [DYNAMIC_SLOT, vol_int])
            finally:
                self._attr_state = MediaPlayerState.IDLE
                self.async_write_ha_state()
