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
GITHUB_BRANCH = "main"
# Canonical firmware manifest: committed by the firmware build repo into this
# integration repo at firmware/manifest.json. Devices fetch the .bin files at
# firmware/bin/<artifact>.bin via raw URLs referenced from the manifest.
MANIFEST_RAW_URL = (
    f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}"
    "/firmware/manifest.json"
)
# Fallback: older releases attached the manifest.json as a release asset.
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

        manifest = await self._fetch_raw_manifest(session, timeout)
        release_url: str | None = None
        release_notes: str | None = None

        # Best-effort enrichment: pick up release_url / notes from the latest
        # GitHub release. Failure here must not block OTA — the manifest is
        # the source of truth.
        try:
            async with session.get(
                GITHUB_RELEASES_URL, timeout=timeout
            ) as resp:
                if resp.status == 200:
                    release = await resp.json()
                    if isinstance(release, dict):
                        release_url = release.get("html_url")
                        release_notes = release.get("body")
                        # Legacy fallback: if the raw fetch failed AND a
                        # release-asset manifest exists, parse it.
                        if not manifest:
                            asset_url = _find_manifest_asset_url(release)
                            if asset_url:
                                manifest = (
                                    await _fetch_json(
                                        session, asset_url, timeout
                                    )
                                    or {}
                                )
        except (aiohttp.ClientError, asyncio.TimeoutError):
            _LOGGER.debug(
                "NUBLY HA: GitHub release lookup failed (non-fatal)"
            )

        if not manifest:
            raise UpdateFailed(
                "Firmware manifest not found at firmware/manifest.json or "
                "in the latest release"
            )

        return {
            "manifest": manifest,
            "release_url": release_url,
            "release_notes": release_notes,
        }

    async def _fetch_raw_manifest(
        self, session, timeout
    ) -> dict:
        """Fetch the canonical firmware/manifest.json from raw.githubusercontent."""
        try:
            async with session.get(
                MANIFEST_RAW_URL, timeout=timeout
            ) as resp:
                if resp.status == 200:
                    parsed = await resp.json(content_type=None)
                    if isinstance(parsed, dict):
                        _LOGGER.debug(
                            "NUBLY HA: manifest fetched from %s",
                            MANIFEST_RAW_URL,
                        )
                        return parsed
                else:
                    _LOGGER.debug(
                        "NUBLY HA: manifest raw fetch HTTP %s",
                        resp.status,
                    )
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            _LOGGER.debug(
                "NUBLY HA: manifest raw fetch failed (will try release fallback)"
            )
        return {}

    def resolve(self, board: str | None) -> FirmwareInfo | None:
        """Return firmware metadata for the given board, or None."""
        data = self.data or {}
        manifest = data.get("manifest") or {}
        if not isinstance(manifest, dict):
            return None

        entry = _find_board_entry(manifest, board)
        info: FirmwareInfo | None = None

        if entry is not None:
            info = FirmwareInfo(
                board=board or entry.get("board") or "",
                version=entry.get("version"),
                firmware_url=entry.get("firmware_url") or entry.get("url"),
                sha256=entry.get("sha256"),
                release_url=data.get("release_url"),
                release_notes=data.get("release_notes"),
            )
        elif manifest.get("version") and not manifest.get("boards") and not manifest.get("firmwares"):
            # Legacy flat manifest, single firmware. Board-agnostic.
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


def _find_board_entry(manifest: dict, board: str | None) -> dict | None:
    """Locate the entry for `board` in either canonical or legacy schemas."""
    if not board:
        return None

    boards = manifest.get("boards")
    if isinstance(boards, dict):
        entry = boards.get(board)
        if isinstance(entry, dict):
            return entry

    # Legacy: { "firmwares": [ { "board": "...", "version": "...", ... }, ... ] }
    firmwares = manifest.get("firmwares")
    if isinstance(firmwares, list):
        for entry in firmwares:
            if isinstance(entry, dict) and entry.get("board") == board:
                return entry

    return None


async def _fetch_json(session, url: str, timeout) -> dict | None:
    try:
        async with session.get(url, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            parsed = await resp.json(content_type=None)
            return parsed if isinstance(parsed, dict) else None
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None


def _find_manifest_asset_url(release: dict) -> str | None:
    for asset in release.get("assets") or []:
        if isinstance(asset, dict) and asset.get("name") == "manifest.json":
            return asset.get("browser_download_url")
    return None
