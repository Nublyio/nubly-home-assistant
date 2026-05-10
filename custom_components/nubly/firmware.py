"""Firmware metadata provider for Nubly OTA updates.

Decouples Update entities from any specific hosting mechanism. Today the
backing source is a `manifest.json` asset on the latest GitHub release, but
nothing outside this module should know that.

Manifest shape (recommended, per-board):

    {
      "boards": {
        "round_1_43":      { "version": "0.1.1", "firmware_url": "...", "sha256": "..." },
        "lcd_5_1024x600":  { "version": "0.2.0", "firmware_url": "...", "sha256": "..." }
      }
    }

Legacy fallback shape (single board, no compat data):

    { "version": "0.1.1", "url": "...", "sha256": "..." }
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

_LOGGER = logging.getLogger(__name__)

GITHUB_REPO = "Nublyio/nubly-home-assistant"
GITHUB_RELEASES_URL = (
    f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
)
SCAN_INTERVAL = timedelta(hours=6)
HTTP_TIMEOUT_SECONDS = 15

# Boards the integration knows how to provision firmware for.
BOARD_ROUND_1_43 = "round_1_43"
BOARD_LCD_5 = "lcd_5_1024x600"
SUPPORTED_BOARDS: frozenset[str] = frozenset({BOARD_ROUND_1_43, BOARD_LCD_5})


@dataclass(frozen=True)
class FirmwareInfo:
    """Resolved firmware metadata for a single board."""

    board: str
    version: str | None
    firmware_url: str | None
    sha256: str | None
    release_url: str | None
    release_notes: str | None


def normalize_board(value) -> str | None:
    """Map noisy device-reported board strings to the canonical id."""
    if not isinstance(value, str):
        return None
    v = value.strip().lower().replace("-", "_")
    if not v:
        return None
    if v in SUPPORTED_BOARDS:
        return v
    if "round" in v:
        return BOARD_ROUND_1_43
    if "lcd" in v or "1024" in v:
        return BOARD_LCD_5
    return None


class NublyFirmwareProvider(DataUpdateCoordinator[dict]):
    """Periodically refreshes the latest firmware manifest."""

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="nubly_firmware_provider",
            update_interval=SCAN_INTERVAL,
        )

    async def _async_update_data(self) -> dict:
        session = async_get_clientsession(self.hass)
        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)

        try:
            async with session.get(
                GITHUB_RELEASES_URL, timeout=timeout
            ) as resp:
                if resp.status != 200:
                    raise UpdateFailed(
                        f"GitHub releases returned HTTP {resp.status}"
                    )
                release = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise UpdateFailed(f"GitHub fetch failed: {err}") from err

        if not isinstance(release, dict):
            raise UpdateFailed("Unexpected GitHub release payload")

        manifest_url = _find_manifest_asset_url(release)
        manifest: dict = {}
        if manifest_url:
            try:
                async with session.get(
                    manifest_url, timeout=timeout
                ) as resp:
                    if resp.status == 200:
                        parsed = await resp.json(content_type=None)
                        if isinstance(parsed, dict):
                            manifest = parsed
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                _LOGGER.debug("NUBLY HA: manifest.json fetch failed")

        return {
            "manifest": manifest,
            "release_url": release.get("html_url"),
            "release_notes": release.get("body"),
            "tag": release.get("tag_name"),
        }

    def resolve(self, board: str | None) -> FirmwareInfo | None:
        """Return firmware metadata for the given board, or None."""
        data = self.data or {}
        manifest = data.get("manifest") or {}
        if not isinstance(manifest, dict):
            return None

        info: FirmwareInfo | None = None
        boards = manifest.get("boards")

        if isinstance(boards, dict) and board and board in boards:
            entry = boards[board]
            if isinstance(entry, dict):
                info = FirmwareInfo(
                    board=board,
                    version=entry.get("version"),
                    firmware_url=entry.get("firmware_url") or entry.get("url"),
                    sha256=entry.get("sha256"),
                    release_url=data.get("release_url"),
                    release_notes=data.get("release_notes"),
                )
        elif manifest.get("version") and not boards:
            # Legacy flat manifest. Treat as board-agnostic and let the
            # caller decide whether to install (compat check still applies
            # at the device side).
            info = FirmwareInfo(
                board=board or "",
                version=manifest.get("version"),
                firmware_url=manifest.get("firmware_url")
                or manifest.get("url"),
                sha256=manifest.get("sha256"),
                release_url=data.get("release_url"),
                release_notes=data.get("release_notes"),
            )

        _LOGGER.debug(
            "NUBLY HA: firmware metadata resolved for board=%s -> %s",
            board,
            info,
        )
        return info


def _find_manifest_asset_url(release: dict) -> str | None:
    for asset in release.get("assets") or []:
        if isinstance(asset, dict) and asset.get("name") == "manifest.json":
            return asset.get("browser_download_url")
    return None
