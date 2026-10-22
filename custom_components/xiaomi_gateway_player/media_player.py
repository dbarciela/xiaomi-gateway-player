from __future__ import annotations

import asyncio
import glob
import hashlib
import json
import logging
import os
import random
import re
import socket
import string
import struct
import threading
import time
from typing import Any
from urllib.parse import quote_plus, urljoin, urlsplit

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
RADIO_M3U8_FILENAME = "gw_radio.m3u8"
RADIO_PROGRAM_ID = 1080
MAX_FLASH_AAC_BYTES = 430000  # ~105s at 32kbps; anything larger auto-streams via HLS!

RADIO_BROWSER_MIRRORS = (
    "https://de1.api.radio-browser.info",
    "https://fi1.api.radio-browser.info",
    "https://at1.api.radio-browser.info",
)

DEFAULT_PT_FAVORITES = [
    "Antena 1",
    "TSF",
    "Rádio Nova Era",
    "Smooth FM",
    "Rádio Comercial",
    "RFM",
]

_MIIO_LOCK = threading.Lock()
_MSG_ID_COUNTER = int(time.time() * 10) % 500000 + 60000


def _miio_send_command(
    host: str,
    token_hex: str,
    method: str,
    params: list[Any] | dict[str, Any],
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
            hello = bytes.fromhex(
                "21310020ffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
            )
            sock.sendto(hello, (host, 54321))
            data, _ = sock.recvfrom(1024)
            dev_id = data[8:12]
            ts = struct.unpack(">I", data[12:16])[0]

            payload_dict = {"id": msg_id, "method": method, "params": params}
            payload_bytes = (
                json.dumps(payload_dict, separators=(",", ":")) + "\x00"
            ).encode("utf-8")

            padder = sym_padding.PKCS7(128).padder()
            padded = padder.update(payload_bytes) + padder.finalize()

            cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
            encryptor = cipher.encryptor()
            encrypted = encryptor.update(padded) + encryptor.finalize()

            length = 32 + len(encrypted)
            header = (
                struct.pack(">HH", 0x2131, length)
                + b"\x00\x00\x00\x00"
                + dev_id
                + struct.pack(">I", ts + 1)
            )
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
    ha_url = entry.options.get(
        "ha_url", entry.data.get("ha_url", "http://192.168.1.150:8123")
    ).rstrip("/")
    name = entry.data.get("name", "Coluna Gateway Hall")

    default_country = (hass.config.country or "PT").upper()
    radio_country = (
        entry.options.get(
            "radio_country", entry.data.get("radio_country", default_country)
        )
        .strip()
        .upper()
    )
    favorite_radios_raw = entry.options.get(
        "favorite_radios", entry.data.get("favorite_radios", "")
    ).strip()

    subsonic_cfg = {
        "url": entry.options.get("subsonic_url", "").strip().rstrip("/"),
        "user": entry.options.get("subsonic_user", "").strip(),
        "password": entry.options.get("subsonic_password", "").strip(),
    }

    player = XiaomiGatewayMediaPlayer(
        hass,
        host,
        token,
        ha_url,
        name,
        radio_country,
        favorite_radios_raw,
        subsonic_cfg,
        entry.entry_id,
    )
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

    async def handle_play_radio(call: ServiceCall) -> None:
        query = call.data["query"]
        country = call.data.get("country")
        volume = call.data.get("volume")
        if volume is not None:
            await player.async_set_volume_level(volume / 100.0)
        await player.async_play_radio_query(query, country=country)

    hass.services.async_register(
        DOMAIN,
        "save_sound",
        handle_save_sound,
        schema=vol.Schema(
            {
                vol.Required("url"): cv.string,
                vol.Optional("slot_id", default="10006"): vol.Coerce(str),
                vol.Optional("play_now", default=True): cv.boolean,
                vol.Optional("volume"): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=100)
                ),
            }
        ),
    )

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

    hass.services.async_register(
        DOMAIN,
        "play_radio",
        handle_play_radio,
        schema=vol.Schema(
            {
                vol.Required("query"): cv.string,
                vol.Optional("country"): cv.string,
                vol.Optional("volume"): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=100)
                ),
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
        | MediaPlayerEntityFeature.SELECT_SOURCE
        | MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_STEP
        | MediaPlayerEntityFeature.VOLUME_MUTE
        | MediaPlayerEntityFeature.STOP
        | MediaPlayerEntityFeature.PAUSE
        | MediaPlayerEntityFeature.PLAY
        | MediaPlayerEntityFeature.TURN_OFF
    )

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        token: str,
        ha_url: str,
        name: str,
        radio_country: str,
        favorite_radios_raw: str,
        subsonic_cfg: dict[str, str],
        entry_id: str,
    ) -> None:
        """Initialize the Xiaomi Gateway media player."""
        self.hass = hass
        self._host = host
        self._token = token
        self._ha_url = ha_url
        self._attr_name = name
        self._radio_country = radio_country
        self._favorite_radios_raw = favorite_radios_raw
        self._subsonic_cfg = subsonic_cfg
        self._custom_radio_urls: dict[str, str] = {}
        self.entity_id = "media_player.hall_gateway"
        self._attr_unique_id = "hall_gateway_media_player"
        self._attr_state = MediaPlayerState.IDLE
        self._attr_volume_level = 0.12  # Safe default (12%)
        self._attr_is_volume_muted = False
        self._attr_media_title: str | None = None
        self._attr_media_image_url: str | None = None
        self._attr_source: str | None = None
        self._attr_source_list: list[str] = self._parse_initial_favorites()
        self._is_hls_streaming = False
        self._radio_proc: asyncio.subprocess.Process | None = None
        self._watcher_task: asyncio.Task[None] | None = None
        self._last_radio_query: str | None = None
        self._attr_extra_state_attributes: dict[str, Any] = {
            "gateway_ip": host,
            "radio_country_filter": self._radio_country or "ALL",
            "subsonic_configured": bool(self._subsonic_cfg.get("url")),
            "dynamic_slot": DYNAMIC_SLOT,
            "flash_slot_max_seconds": 105,
            "hls_stream_max_seconds": "unlimited",
            "stored_slots": [],
            "free_space_bytes": None,
            "last_transfer_ms": None,
            "playback_mode": "idle",
        }
        self._lock = asyncio.Lock()

    def _parse_initial_favorites(self) -> list[str]:
        """Parse user-configured favorite radio names (and optional Name|URL pairs)."""
        if self._favorite_radios_raw:
            items: list[str] = []
            for part in re.split(r"[,\n;]+", self._favorite_radios_raw):
                part = part.strip()
                if not part:
                    continue
                if "|" in part:
                    label, url = part.split("|", 1)
                    label = label.strip()
                    self._custom_radio_urls[label] = url.strip()
                    items.append(label)
                else:
                    items.append(part)
            if items:
                return items
        if self._radio_country == "PT":
            return list(DEFAULT_PT_FAVORITES)
        return []

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

    async def async_will_remove_from_hass(self) -> None:
        """Clean up background ffmpeg process when entity is unloaded."""
        await self._async_stop_radio_process()

    async def _send_miio(
        self,
        method: str,
        params: list[Any] | dict[str, Any],
        timeout: float = 2.5,
    ) -> Any:
        return await self.hass.async_add_executor_job(
            _miio_send_command, self._host, self._token, method, params, timeout
        )

    async def async_update(self) -> None:
        """Fetch stored custom slots, free flash space, and populate country radios if needed."""
        if not self._attr_source_list and self._radio_country:
            try:
                top_stations = await self._async_fetch_top_country_stations(
                    self._radio_country
                )
                if top_stations:
                    self._attr_source_list = top_stations
            except Exception as err:
                _LOGGER.debug(
                    "Could not fetch top stations for %s: %s", self._radio_country, err
                )

        try:
            info_resp = await self._send_miio("get_music_info", [0], timeout=2.5)
            if info_resp and "result" in info_resp:
                res = info_resp["result"]
                if isinstance(res, list) and res:
                    res = res[0]
                if isinstance(res, dict):
                    slots = [
                        {
                            "slot_id": str(item.get("mid")),
                            "duration_s": item.get("time"),
                        }
                        for item in res.get("list", [])
                        if int(item.get("mid", 0)) >= 10000
                    ]
                    self._attr_extra_state_attributes["stored_slots"] = slots

            space_resp = await self._send_miio("get_music_free_space", [], timeout=2.5)
            if space_resp and "result" in space_resp and space_resp["result"]:
                self._attr_extra_state_attributes["free_space_bytes"] = space_resp[
                    "result"
                ][0]

            fm_resp = await self._send_miio("get_prop_fm", [], timeout=2.5)
            if fm_resp and "result" in fm_resp and isinstance(fm_resp["result"], dict):
                fm_res = fm_resp["result"]
                self._attr_extra_state_attributes["gateway_fm_status"] = fm_res.get("current_status")
                self._attr_extra_state_attributes["gateway_fm_volume"] = fm_res.get("current_volume")
                self._attr_extra_state_attributes["gateway_fm_program"] = fm_res.get("current_program")
        except Exception as err:
            _LOGGER.debug("Could not refresh Xiaomi Gateway music info: %s", err)

    async def _async_fetch_top_country_stations(self, country_code: str) -> list[str]:
        """Fetch top 8 clicked stations for a country from Radio Browser API."""
        session = async_get_clientsession(self.hass)
        headers = {"User-Agent": "HomeAssistant-XiaomiGatewayPlayer/1.1"}
        for mirror in RADIO_BROWSER_MIRRORS:
            url = (
                f"{mirror}/json/stations/search?"
                f"countrycode={quote_plus(country_code)}&hidebroken=true"
                f"&order=clickcount&reverse=true&limit=8"
            )
            try:
                async with session.get(
                    url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        names: list[str] = []
                        for item in data:
                            n = (item.get("name") or "").strip()
                            if n and n not in names:
                                names.append(n)
                        if names:
                            return names
            except Exception:
                continue
        return []

    async def _async_search_radio_browser(
        self, query: str, country_override: str | None = None
    ) -> tuple[str, str, str | None]:
        """Search Radio Browser API for a station by name and optional country code.

        Returns (stream_url, station_name, favicon_url).
        """
        query = query.strip()
        if query in self._custom_radio_urls:
            return self._custom_radio_urls[query], query, None

        if query.startswith(("http://", "https://")):
            return query, query, None

        # Support inline country syntax: "BBC Radio 1 [GB]"
        inline_cc = country_override
        m = re.match(r"^(.*?)\s*\[([A-Za-z]{2})\]$", query)
        if m:
            query = m.group(1).strip()
            inline_cc = m.group(2).upper()

        cc = (
            (inline_cc if inline_cc is not None else self._radio_country)
            .strip()
            .upper()
        )
        session = async_get_clientsession(self.hass)
        headers = {"User-Agent": "HomeAssistant-XiaomiGatewayPlayer/1.1"}

        search_attempts = [cc, ""] if cc else [""]
        for attempt_cc in search_attempts:
            cc_param = f"&countrycode={quote_plus(attempt_cc)}" if attempt_cc else ""
            for mirror in RADIO_BROWSER_MIRRORS:
                url = (
                    f"{mirror}/json/stations/search?"
                    f"name={quote_plus(query)}{cc_param}&hidebroken=true"
                    f"&order=clickcount&reverse=true&limit=5"
                )
                try:
                    async with session.get(
                        url, headers=headers, timeout=aiohttp.ClientTimeout(total=6)
                    ) as resp:
                        if resp.status != 200:
                            continue
                        results = await resp.json()
                        for station in results:
                            stream_url = (
                                station.get("url_resolved") or station.get("url") or ""
                            ).strip()
                            if stream_url:
                                st_name = (station.get("name") or query).strip()
                                favicon = (station.get("favicon") or "").strip() or None
                                return stream_url, st_name, favicon
                        break
                except Exception:
                    continue

        raise ValueError(
            f"Nenhuma estação de rádio encontrada no Radio Browser para '{query}'"
            + (f" (país: {cc})" if cc else "")
        )

    def _subsonic_auth_params(self) -> str:
        """Build Subsonic REST API authentication query string (salt + md5 token)."""
        user = self._subsonic_cfg.get("user", "")
        password = self._subsonic_cfg.get("password", "")
        salt = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        token = hashlib.md5((password + salt).encode("utf-8")).hexdigest()
        return (
            f"u={quote_plus(user)}&t={token}&s={salt}&v=1.16.1&c=XiaomiGatewayHA&f=json"
        )

    async def _async_resolve_subsonic(
        self, spec: str
    ) -> tuple[list[str], str, str | None]:
        """Resolve a 'subsonic:...' query into a list of stream URLs, title, and cover art URL.

        Supported formats:
        - subsonic:album:<album query>
        - subsonic:artist:<artist query>
        - subsonic:song:<song query>
        - subsonic:playlist:<playlist name>
        - subsonic:random[:<count>]
        """
        base_url = self._subsonic_cfg.get("url", "")
        if not base_url:
            raise ValueError(
                "Servidor Subsonic/Navidrome não está configurado nas opções da integração."
            )

        auth = self._subsonic_auth_params()
        session = async_get_clientsession(self.hass)

        parts = spec.split(":", 2)
        mode = parts[1].strip().lower() if len(parts) > 1 else "random"
        query = parts[2].strip() if len(parts) > 2 else ""

        if mode == "random":
            count = int(query) if query.isdigit() else 25
            api_url = f"{base_url}/rest/getRandomSongs.view?size={count}&{auth}"
            async with session.get(
                api_url, timeout=aiohttp.ClientTimeout(total=8)
            ) as r:
                data = await r.json()
            songs = (
                data.get("subsonic-response", {}).get("randomSongs", {}).get("song", [])
            )
            if not songs:
                raise ValueError("Nenhuma música retornada pelo Subsonic (random).")
            urls = [
                f"{base_url}/rest/stream.view?id={s['id']}&{self._subsonic_auth_params()}"
                for s in songs
            ]
            return urls, f"Subsonic Mix ({len(urls)} faixas)", None

        if mode == "playlist":
            api_url = f"{base_url}/rest/getPlaylists.view?{auth}"
            async with session.get(
                api_url, timeout=aiohttp.ClientTimeout(total=8)
            ) as r:
                data = await r.json()
            playlists = (
                data.get("subsonic-response", {})
                .get("playlists", {})
                .get("playlist", [])
            )
            pl_id = None
            pl_name = query
            for pl in playlists:
                if query.lower() in (pl.get("name") or "").lower():
                    pl_id = pl["id"]
                    pl_name = pl.get("name", query)
                    break
            if not pl_id:
                raise ValueError(f"Playlist Subsonic não encontrada: {query}")
            pl_url = f"{base_url}/rest/getPlaylist.view?id={pl_id}&{auth}"
            async with session.get(pl_url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                pl_data = await r.json()
            entries = (
                pl_data.get("subsonic-response", {})
                .get("playlist", {})
                .get("entry", [])
            )
            urls = [
                f"{base_url}/rest/stream.view?id={e['id']}&{self._subsonic_auth_params()}"
                for e in entries
            ]
            return urls, f"Playlist: {pl_name}", None

        # Search3 for album, artist, or song
        search_url = f"{base_url}/rest/search3.view?query={quote_plus(query)}&{auth}"
        async with session.get(search_url, timeout=aiohttp.ClientTimeout(total=8)) as r:
            s_data = await r.json()
        sr = s_data.get("subsonic-response", {}).get("searchResult3", {})

        if mode == "album":
            albums = sr.get("album", [])
            if not albums:
                raise ValueError(f"Álbum não encontrado no Subsonic: {query}")
            alb = albums[0]
            alb_url = f"{base_url}/rest/getAlbum.view?id={alb['id']}&{auth}"
            async with session.get(
                alb_url, timeout=aiohttp.ClientTimeout(total=8)
            ) as r:
                a_data = await r.json()
            songs = a_data.get("subsonic-response", {}).get("album", {}).get("song", [])
            urls = [
                f"{base_url}/rest/stream.view?id={s['id']}&{self._subsonic_auth_params()}"
                for s in songs
            ]
            cover = (
                f"{base_url}/rest/getCoverArt.view?id={alb['id']}&{auth}"
                if alb.get("coverArt")
                else None
            )
            title = f"{alb.get('artist', '')} - {alb.get('name', query)}".strip(" -")
            return urls, title, cover

        if mode == "artist":
            songs = sr.get("song", [])
            if not songs:
                raise ValueError(
                    f"Músicas do artista não encontradas no Subsonic: {query}"
                )
            urls = [
                f"{base_url}/rest/stream.view?id={s['id']}&{self._subsonic_auth_params()}"
                for s in songs
            ]
            return urls, f"Artista: {query}", None

        # Default: single song
        songs = sr.get("song", [])
        if not songs:
            raise ValueError(f"Música não encontrada no Subsonic: {query}")
        s0 = songs[0]
        url = (
            f"{base_url}/rest/stream.view?id={s0['id']}&{self._subsonic_auth_params()}"
        )
        cover = (
            f"{base_url}/rest/getCoverArt.view?id={s0['coverArt']}&{auth}"
            if s0.get("coverArt")
            else None
        )
        title = f"{s0.get('artist', '')} - {s0.get('title', query)}".strip(" -")
        return [url], title, cover

    async def _async_parse_playlist_if_needed(self, url_or_path: str) -> list[str]:
        """If url_or_path is a multi-track .m3u or .pls playlist (not an HLS segment stream), extract track URLs."""
        lower = url_or_path.lower().split("?")[0]
        if not lower.endswith((".m3u", ".m3u8", ".pls")):
            return [url_or_path]

        try:
            if url_or_path.startswith(("http://", "https://")):
                session = async_get_clientsession(self.hass)
                async with session.get(
                    url_or_path, timeout=aiohttp.ClientTimeout(total=6)
                ) as resp:
                    text = await resp.text()
            elif os.path.exists(url_or_path):
                with open(url_or_path, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read()
            else:
                return [url_or_path]
        except Exception:
            return [url_or_path]

        # If it's a live HLS stream (#EXT-X-TARGETDURATION or #EXT-X-STREAM-INF), pass directly to ffmpeg
        if "#EXT-X-TARGETDURATION" in text or "#EXT-X-STREAM-INF" in text:
            return [url_or_path]

        tracks: list[str] = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if lower.endswith(".pls"):
                m = re.match(r"^File\d+=(.+)$", line, re.IGNORECASE)
                if m:
                    line = m.group(1).strip()
                else:
                    continue
            if line.startswith(("http://", "https://", "/")):
                tracks.append(line)
            elif url_or_path.startswith(("http://", "https://")):
                tracks.append(urljoin(url_or_path, line))

        return tracks if tracks else [url_or_path]

    async def _async_stop_radio_process(self) -> None:
        """Stop the background ffmpeg HLS transcoder and watcher task if running."""
        watcher = self._watcher_task
        self._watcher_task = None
        if watcher is not None and not watcher.done():
            watcher.cancel()

        proc = self._radio_proc
        self._radio_proc = None
        self._is_hls_streaming = False
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    async def _async_start_hls_stream(
        self,
        sources: str | list[str],
        title: str,
        favicon: str | None = None,
        is_live: bool = True,
    ) -> None:
        """Transcode any live stream, long TTS, M3U playlist, or Subsonic album into a local HLS ring buffer."""
        await self._async_stop_radio_process()
        t0 = time.monotonic()

        www_dir = self.hass.config.path("www")

        def _cleanup_old_segments() -> None:
            os.makedirs(www_dir, exist_ok=True)
            for old_file in glob.glob(os.path.join(www_dir, "gw_radio*")):
                try:
                    os.remove(old_file)
                except OSError:
                    pass

        await self.hass.async_add_executor_job(_cleanup_old_segments)

        m3u8_path = os.path.join(www_dir, RADIO_M3U8_FILENAME)
        seg_pattern = os.path.join(www_dir, "gw_radio_%d.aac")

        source_list = [sources] if isinstance(sources, str) else list(sources)
        if len(source_list) == 1:
            source_list = await self._async_parse_playlist_if_needed(source_list[0])

        cmd = ["ffmpeg", "-y"]
        if not is_live:
            cmd.append("-re")

        if len(source_list) > 1:
            concat_path = "/tmp/gw_concat_playlist.txt"

            def _write_concat() -> None:
                with open(concat_path, "w", encoding="utf-8") as f:
                    f.write("ffconcat version 1.0\n")
                    for item in source_list:
                        escaped = item.replace("'", "'\\''")
                        f.write(f"file '{escaped}'\n")

            await self.hass.async_add_executor_job(_write_concat)
            cmd.extend(
                [
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-protocol_whitelist",
                    "file,http,https,tcp,tls,crypto",
                    "-i",
                    concat_path,
                ]
            )
        else:
            single_src = source_list[0]
            if single_src.startswith(("http://", "https://")):
                cmd.extend(
                    [
                        "-reconnect",
                        "1",
                        "-reconnect_streamed",
                        "1",
                        "-reconnect_delay_max",
                        "5",
                    ]
                )
            cmd.extend(["-i", single_src])

        cmd.extend(
            [
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
                "segment",
                "-segment_time",
                "3",
                "-segment_format",
                "adts",
                "-segment_list_size",
                "6",
                "-segment_wrap",
                "20",
                "-segment_list_flags",
                "+live",
                "-segment_list",
                m3u8_path,
                "-segment_list_type",
                "m3u8",
                seg_pattern,
            ]
        )

        self._radio_proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        # Wait until at least 2 full segments (gw_radio_0.aac and gw_radio_1.aac) are in gw_radio.m3u8
        seg2 = os.path.join(www_dir, "gw_radio_2.aac")
        for _ in range(55):
            await asyncio.sleep(0.15)
            if os.path.exists(m3u8_path) and os.path.exists(seg2):
                break

        vol_int = (
            0
            if self._attr_is_volume_muted
            else max(1, int(round(self._attr_volume_level * 100)))
        )
        await self._send_miio("volume_ctrl_fm", [str(vol_int)])
        hls_url = f"{self._ha_url}/local/{RADIO_M3U8_FILENAME}"
        await self._send_miio(
            "play_specify_fm",
            {"id": RADIO_PROGRAM_ID, "type": 0, "url": hls_url},
        )

        elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
        self._is_hls_streaming = True
        self._attr_state = MediaPlayerState.PLAYING
        self._attr_media_title = title
        self._attr_media_image_url = favicon
        self._attr_extra_state_attributes["last_transfer_ms"] = elapsed_ms
        self._attr_extra_state_attributes["last_resolved_url"] = (
            source_list[0]
            if len(source_list) == 1
            else f"playlist ({len(source_list)} tracks)"
        )
        self._attr_extra_state_attributes["playback_mode"] = (
            "hls_live_radio" if is_live else "hls_stream"
        )
        self.async_write_ha_state()

        # For finite streams (long TTS, songs, Subsonic albums), stop gateway FM when ffmpeg finishes
        if not is_live and self._radio_proc is not None:
            proc_ref = self._radio_proc

            async def _watch_completion() -> None:
                try:
                    await proc_ref.wait()
                    await asyncio.sleep(4.5)
                    if self._radio_proc is proc_ref:
                        await self._send_miio("play_fm", ["off"])
                        self._is_hls_streaming = False
                        self._radio_proc = None
                        self._attr_state = MediaPlayerState.IDLE
                        self._attr_extra_state_attributes["playback_mode"] = "idle"
                        self.async_write_ha_state()
                except asyncio.CancelledError:
                    pass
                except Exception as err:
                    _LOGGER.debug("HLS stream watcher error: %s", err)

            self._watcher_task = self.hass.async_create_task(_watch_completion())

    async def async_select_source(self, source: str) -> None:
        """Select and play a favorite radio station from the source dropdown."""
        await self.async_play_radio_query(source)

    async def async_play_radio_query(
        self, query: str, country: str | None = None
    ) -> None:
        """Search and play a live radio station on the Xiaomi Gateway."""
        async with self._lock:
            self._last_radio_query = query
            stream_url, st_name, favicon = await self._async_search_radio_browser(
                query, country_override=country
            )
            self._attr_source = st_name
            await self._async_start_hls_stream(
                stream_url, f"📻 {st_name}", favicon, is_live=True
            )

    async def async_set_volume_level(self, volume: float) -> None:
        """Set volume level, range 0..1, and update live FM/HLS volume if streaming."""
        self._attr_volume_level = max(0.0, min(1.0, volume))
        if self._is_hls_streaming and not self._attr_is_volume_muted:
            vol_int = max(1, int(round(self._attr_volume_level * 100)))
            try:
                await self._send_miio("volume_ctrl_fm", [str(vol_int)])
            except Exception as err:
                _LOGGER.debug("Failed to update live FM volume: %s", err)
        self.async_write_ha_state()

    async def async_mute_volume(self, mute: bool) -> None:
        """Mute or unmute volume."""
        self._attr_is_volume_muted = mute
        if mute:
            await self.async_media_stop()
        self.async_write_ha_state()

    async def async_media_stop(self) -> None:
        """Stop currently playing sound or live HLS stream on the gateway."""
        await self._async_stop_radio_process()
        try:
            await self._send_miio("play_fm", ["off"])
            await self._send_miio("set_sound_playing", ["off"])
        finally:
            self._attr_state = MediaPlayerState.IDLE
            self._attr_media_image_url = None
            self._attr_extra_state_attributes["playback_mode"] = "idle"
            self.async_write_ha_state()

    async def async_media_pause(self) -> None:
        """Pause stops live radio or sound playback."""
        await self.async_media_stop()

    async def async_media_play(self) -> None:
        """Resume last played radio station if available."""
        if self._last_radio_query:
            await self.async_play_radio_query(self._last_radio_query)

    async def async_turn_off(self) -> None:
        """Turn off stops playback."""
        await self.async_media_stop()

    async def async_browse_media(
        self,
        media_content_type: MediaType | str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        """Implement the websocket media browsing helper (enables Radio Browser, TTS & Local Media in UI)."""
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
                local_file = self.hass.config.path("www", raw_url[len("/local/") :])
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
            search_url = (
                f"https://www.myinstants.com/pt/search/?name={quote_plus(query)}"
            )
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
        if "myinstants.com" in media_id and not media_id.lower().split("?")[0].endswith(
            ".mp3"
        ):
            session = async_get_clientsession(self.hass)
            async with session.get(
                media_id, headers=headers, timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                html = await resp.text()
            match = re.search(r"(/media/sounds/[^\"']+\.mp3)", html)
            if not match:
                raise ValueError(
                    f"Não foi possível extrair o MP3 da página MyInstants: {media_id}"
                )
            return f"https://www.myinstants.com{match.group(1)}"

        # 4. Relative /local/ path
        if media_id.startswith("/local/"):
            local_file = self.hass.config.path("www", media_id[len("/local/") :])
            if os.path.exists(local_file):
                return local_file
            return f"http://127.0.0.1:8123{media_id}"

        return media_id

    async def _convert_and_upload(
        self,
        source_url: str,
        slot_id: str,
        out_filename: str,
        allow_hls_fallback: bool = True,
    ) -> tuple[float, bool]:
        """Convert audio to 22050Hz mono 32kbps ADTS AAC and upload to gateway flash slot.

        Returns (elapsed_ms, used_hls_fallback).
        If the converted file exceeds MAX_FLASH_AAC_BYTES (~105s) and allow_hls_fallback=True,
        automatically streams the full audio via HLS instead of truncating!
        """
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
                source_url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                resp.raise_for_status()
                data = await resp.read()
            with open(tmp_input, "wb") as f:
                f.write(data)
            input_target = tmp_input

        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            input_target,
        ]
        if not allow_hls_fallback:
            cmd.extend(["-t", "110"])
        cmd.extend(
            [
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
            ]
        )

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            err_msg = stderr.decode("utf-8", errors="ignore")[-300:]
            raise RuntimeError(f"Erro ffmpeg ao converter áudio: {err_msg}")

        # Automatic HLS fallback for long TTS / full songs (> 105 seconds / 430 KB)
        if (
            allow_hls_fallback
            and os.path.exists(out_path)
            and os.path.getsize(out_path) > MAX_FLASH_AAC_BYTES
        ):
            await self._async_start_hls_stream(
                input_target, self._attr_media_title or "Stream", is_live=False
            )
            return round((time.monotonic() - t0) * 1000, 1), True

        # Tell gateway to download from Home Assistant HTTP /local/
        aac_url = f"{self._ha_url}/local/{out_filename}"
        await self._send_miio(
            "download_user_music", [str(slot_id), aac_url], timeout=3.0
        )

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
        self._attr_extra_state_attributes["playback_mode"] = "flash_slot"
        return elapsed_ms, False

    async def async_save_to_slot(
        self,
        url: str,
        slot_id: str,
        play_now: bool = True,
        volume: int | None = None,
    ) -> None:
        """Save any audio URL or MyInstants link permanently into a gateway slot (10001..10029)."""
        async with self._lock:
            await self._async_stop_radio_process()
            resolved = await self._resolve_source_url(url)
            filename = f"gw_slot_{slot_id}.aac"
            await self._convert_and_upload(
                resolved, slot_id, filename, allow_hls_fallback=False
            )
            await self.async_update()
            if play_now:
                vol_int = (
                    volume
                    if volume is not None
                    else max(1, int(round(self._attr_volume_level * 100)))
                )
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
        """Play a piece of media (slot ID, live radio, Subsonic, M3U playlist, TTS, MyInstants, or local/remote audio)."""
        clean_id = media_id.strip()
        lower_id = clean_id.lower()

        # 1. Live radio via "radio:<station>" prefix
        if lower_id.startswith("radio:"):
            query = clean_id.split(":", 1)[1].strip()
            await self.async_play_radio_query(query)
            return

        # 2. Subsonic / Navidrome query ("subsonic:album:...", "subsonic:song:...", "subsonic:random")
        if lower_id.startswith("subsonic:"):
            async with self._lock:
                urls, title, cover = await self._async_resolve_subsonic(clean_id)
                await self._async_start_hls_stream(urls, title, cover, is_live=False)
            return

        # 3. Explicit HLS stream or M3U/PLS playlist ("stream:<url>", "*.m3u", "*.m3u8", "*.pls")
        if lower_id.startswith(("stream:", "playlist:")) or lower_id.split("?")[
            0
        ].endswith((".m3u", ".m3u8", ".pls")):
            raw_target = (
                clean_id.split(":", 1)[1].strip()
                if lower_id.startswith(("stream:", "playlist:"))
                else clean_id
            )
            async with self._lock:
                resolved = await self._resolve_source_url(raw_target)
                await self._async_start_hls_stream(
                    resolved, raw_target, is_live=lower_id.startswith("stream:")
                )
            return

        # 4. Live radio selected from Home Assistant's built-in Radio Browser (media-source://radio_browser/...)
        if "radio_browser" in lower_id or str(media_type).lower() in (
            "radio",
            "channel",
        ):
            async with self._lock:
                sourced_media = await media_source.async_resolve_media(
                    self.hass, clean_id, self.entity_id
                )
                await self._async_start_hls_stream(
                    sourced_media.url, clean_id.split("/")[-1], is_live=True
                )
            return

        # 5. Short audio / TTS / MyInstants / Cached Slot (with automatic HLS fallback if > 105s!)
        async with self._lock:
            await self._async_stop_radio_process()
            self._attr_state = MediaPlayerState.PLAYING
            self._attr_media_title = clean_id
            self._attr_media_image_url = None
            self.async_write_ha_state()

            vol_int = (
                0
                if self._attr_is_volume_muted
                else max(1, int(round(self._attr_volume_level * 100)))
            )

            try:
                # Direct slot playback (e.g. "10005", "slot:10005", "ringtone:0") -> <50ms!
                for prefix in ("slot:", "ringtone:"):
                    if clean_id.lower().startswith(prefix):
                        clean_id = clean_id.split(":", 1)[1].strip()

                if clean_id.isdigit():
                    t0 = time.monotonic()
                    await self._send_miio("play_music_new", [str(clean_id), vol_int])
                    self._attr_extra_state_attributes["last_transfer_ms"] = round(
                        (time.monotonic() - t0) * 1000, 1
                    )
                    self._attr_extra_state_attributes["playback_mode"] = "flash_slot"
                    self._attr_state = MediaPlayerState.IDLE
                    self.async_write_ha_state()
                else:
                    resolved_url = await self._resolve_source_url(clean_id)
                    self._attr_extra_state_attributes["last_resolved_url"] = (
                        resolved_url
                    )
                    _, used_hls = await self._convert_and_upload(
                        resolved_url,
                        DYNAMIC_SLOT,
                        DYNAMIC_AAC_FILENAME,
                        allow_hls_fallback=True,
                    )
                    if not used_hls:
                        await self._send_miio("play_music_new", [DYNAMIC_SLOT, vol_int])
                        self._attr_state = MediaPlayerState.IDLE
                        self.async_write_ha_state()
            except Exception:
                self._attr_state = MediaPlayerState.IDLE
                self.async_write_ha_state()
                raise
