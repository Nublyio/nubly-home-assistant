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
import json
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
                _LOGGER.debug(
                    "NUBLY HA: GitHub releases fetch %s -> HTTP %s",
                    GITHUB_RELEASES_URL,
                    resp.status,
                )
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
                                _LOGGER.debug(
                                    "NUBLY HA: falling back to release-asset "
                                    "manifest at %s",
                                    asset_url,
                                )
                                manifest = (
                                    await _fetch_json(
                                        session, asset_url, timeout
                                    )
                                    or {}
                                )
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.debug(
                "NUBLY HA: GitHub release lookup failed (non-fatal): %s", err
            )

        if not manifest:
            raise UpdateFailed(
                f"Firmware manifest not found at {MANIFEST_RAW_URL} "
                "or in the latest GitHub release"
            )

        _LOGGER.debug(
            "NUBLY HA: manifest top-level keys=%s, boards=%s",
            sorted(manifest.keys()),
            sorted((manifest.get("boards") or {}).keys())
            if isinstance(manifest.get("boards"), dict)
            else None,
        )

        return {
            "manifest": manifest,
            "release_url": release_url,
            "release_notes": release_notes,
        }

    async def _fetch_raw_manifest(self, session, timeout) -> dict:
        """Fetch the canonical firmware/manifest.json from raw.githubusercontent."""
        _LOGGER.debug(
            "NUBLY HA: fetching manifest from %s", MANIFEST_RAW_URL
        )
        try:
            async with session.get(
                MANIFEST_RAW_URL, timeout=timeout
            ) as resp:
                status = resp.status
                if status == 200:
                    text = await resp.text()
                    _LOGGER.debug(
                        "NUBLY HA: manifest raw fetch HTTP 200, %d bytes",
                        len(text),
                    )
                    try:
                        parsed = json.loads(text)
                    except (ValueError, TypeError) as err:
                        _LOGGER.warning(
                            "NUBLY HA: manifest JSON parse failed: %s", err
                        )
                        return {}
                    if isinstance(parsed, dict):
                        return parsed
                    _LOGGER.warning(
                        "NUBLY HA: manifest is not a JSON object (type=%s)",
                        type(parsed).__name__,
                    )
                    return {}
                _LOGGER.warning(
                    "NUBLY HA: manifest raw fetch HTTP %s from %s",
                    status,
                    MANIFEST_RAW_URL,
                )
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.warning(
                "NUBLY HA: manifest raw fetch failed: %s (will try release fallback)",
                err,
            )
        except Exception:
            _LOGGER.exception(
                "NUBLY HA: unexpected error fetching manifest from %s",
                MANIFEST_RAW_URL,
            )
        return {}

    def versions_for(self, board: str | None) -> list[str]:
        """Return every manifest version string available for the given board."""
        data = self.data or {}
        manifest = data.get("manifest") or {}
        if not isinstance(manifest, dict) or not board:
            return []
        return [
            e["version"]
            for e in _board_entries(manifest, board)
            if isinstance(e.get("version"), str) and e["version"]
        ]

    def resolve(self, board: str | None) -> FirmwareInfo | None:
        """Return firmware metadata for the given board, or None.

        When the manifest contains multiple entries for the same board, the
        entry with the highest semantic version wins.
        """
        _LOGGER.debug("NUBLY HA: resolve() requested for board=%s", board)
        data = self.data or {}
        manifest = data.get("manifest") or {}
        if not isinstance(manifest, dict):
            _LOGGER.debug(
                "NUBLY HA: resolve() — coordinator has no manifest yet"
            )
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


def _board_entries(manifest: dict, board: str | None) -> list[dict]:
    """Return every firmware entry for `board` across all supported schemas.

    Handles:
      - canonical single:   manifest["boards"][board] = {version, firmware_url, sha256}
      - canonical multi:    manifest["boards"][board] = [ {version, ...}, ... ]
      - canonical multi v2: manifest["boards"][board] = {"versions": [ ... ]}
      - legacy:             manifest["firmwares"] = [ {board, version, ...}, ... ]
    """
    if not board:
        return []
    entries: list[dict] = []

    boards = manifest.get("boards")
    if isinstance(boards, dict):
        node = boards.get(board)
        if isinstance(node, list):
            entries.extend(e for e in node if isinstance(e, dict))
        elif isinstance(node, dict):
            versions = node.get("versions")
            if isinstance(versions, list):
                entries.extend(e for e in versions if isinstance(e, dict))
            else:
                entries.append(node)

    firmwares = manifest.get("firmwares")
    if isinstance(firmwares, list):
        for entry in firmwares:
            if isinstance(entry, dict) and entry.get("board") == board:
                entries.append(entry)

    return entries


def _find_board_entry(manifest: dict, board: str | None) -> dict | None:
    """Return the entry with the highest semantic version for `board`."""
    entries = _board_entries(manifest, board)
    return _pick_newest(entries)


def _pick_newest(entries: list[dict]) -> dict | None:
    """Pick the entry with the highest semantic version. Stable for ties."""
    candidates = [
        e for e in entries
        if isinstance(e.get("version"), str) and e["version"]
    ]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    try:
        from awesomeversion import AwesomeVersion

        return max(candidates, key=lambda e: AwesomeVersion(e["version"]))
    except Exception:
        # Fallback if AwesomeVersion is unavailable or a version is unparseable.
        return max(candidates, key=lambda e: _version_tuple(e["version"]))


def _version_tuple(version: str) -> tuple:
    """Best-effort numeric tuple for semver-ish strings."""
    core = version.split("-", 1)[0].split("+", 1)[0]
    out: list[int] = []
    for part in core.split("."):
        try:
            out.append(int(part))
        except ValueError:
            out.append(0)
    return tuple(out)


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
