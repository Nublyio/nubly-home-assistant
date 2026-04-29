"""Nubly firmware UpdateEntity backed by GitHub Releases.

A `manifest.json` asset attached to the latest GitHub release is the source
of truth for the firmware update. It must contain at least:

    {
      "version": "1.2.3",
      "url":     "https://example.com/firmware.bin",
      "sha256":  "<hex digest>"
    }

If the asset is missing or incomplete, no update is exposed (the entity
simply shows no available version).
"""

import asyncio
import json
import logging
from datetime import timedelta

import aiohttp

from homeassistant.components import mqtt
from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import CONF_DEVICE_ID, CONF_SW_VERSION, DOMAIN

_LOGGER = logging.getLogger(__name__)

GITHUB_REPO = "Nublyio/nubly-home-assistant"
GITHUB_RELEASES_URL = (
    f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
)
SCAN_INTERVAL = timedelta(hours=6)
HTTP_TIMEOUT_SECONDS = 15


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Nubly firmware update entity for this config entry."""
    coordinator = hass.data[DOMAIN].get("_release_coordinator")
    if coordinator is None:
        coordinator = NublyReleaseCoordinator(hass)
        hass.data[DOMAIN]["_release_coordinator"] = coordinator
        await coordinator.async_config_entry_first_refresh()

    async_add_entities([NublyFirmwareUpdate(hass, entry, coordinator)])


class NublyReleaseCoordinator(DataUpdateCoordinator):
    """Periodically fetches the latest firmware manifest from GitHub."""

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="nubly_firmware_release",
            update_interval=SCAN_INTERVAL,
        )

    async def _async_update_data(self) -> dict:
        session = async_get_clientsession(self.hass)
        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)

        try:
            async with session.get(GITHUB_RELEASES_URL, timeout=timeout) as resp:
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
        manifest = {}
        if manifest_url:
            try:
                async with session.get(
                    manifest_url, timeout=timeout
                ) as resp:
                    if resp.status == 200:
                        manifest = await resp.json(content_type=None)
                        if not isinstance(manifest, dict):
                            manifest = {}
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                _LOGGER.debug(
                    "NUBLY HA: failed to fetch firmware manifest.json"
                )

        return {
            "version": manifest.get("version"),
            "url": manifest.get("url"),
            "sha256": manifest.get("sha256"),
            "release_url": release.get("html_url"),
            "release_notes": release.get("body"),
            "tag": release.get("tag_name"),
        }


def _find_manifest_asset_url(release: dict) -> str | None:
    for asset in release.get("assets") or []:
        if isinstance(asset, dict) and asset.get("name") == "manifest.json":
            return asset.get("browser_download_url")
    return None


class NublyFirmwareUpdate(CoordinatorEntity[NublyReleaseCoordinator], UpdateEntity):
    """One firmware UpdateEntity per Nubly device."""

    _attr_supported_features = (
        UpdateEntityFeature.INSTALL | UpdateEntityFeature.RELEASE_NOTES
    )
    _attr_has_entity_name = True
    _attr_name = "Firmware"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: NublyReleaseCoordinator,
    ) -> None:
        super().__init__(coordinator)
        self.hass = hass
        self._entry = entry
        self._device_id = entry.data[CONF_DEVICE_ID]
        self._attr_unique_id = f"{self._device_id}_firmware"
        self._installed_version: str | None = entry.data.get(CONF_SW_VERSION)
        self._unsub_attrs = None

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._device_id)})

    @property
    def installed_version(self) -> str | None:
        return self._installed_version

    @property
    def latest_version(self) -> str | None:
        data = self.coordinator.data or {}
        return data.get("version")

    @property
    def release_url(self) -> str | None:
        data = self.coordinator.data or {}
        return data.get("release_url")

    async def async_release_notes(self) -> str | None:
        data = self.coordinator.data or {}
        return data.get("release_notes")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        topic = f"nubly/devices/{self._device_id}/attributes"

        @callback
        def on_attrs(msg) -> None:
            payload = msg.payload
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8", errors="replace")
            try:
                data = json.loads(payload)
            except (json.JSONDecodeError, TypeError, ValueError):
                return
            if not isinstance(data, dict):
                return
            sw = data.get("sw_version")
            if isinstance(sw, str) and sw and sw != self._installed_version:
                self._installed_version = sw
                self.async_write_ha_state()

        try:
            self._unsub_attrs = await mqtt.async_subscribe(
                self.hass, topic, on_attrs
            )
        except Exception:
            _LOGGER.exception(
                "NUBLY HA: failed to subscribe to %s for sw_version updates",
                topic,
            )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_attrs is not None:
            self._unsub_attrs()
            self._unsub_attrs = None

    async def async_install(
        self, version: str | None, backup: bool, **kwargs
    ) -> None:
        """Tell the ESP32 to install the latest firmware over MQTT."""
        data = self.coordinator.data or {}
        target_version = version or data.get("version")
        url = data.get("url")
        sha256 = data.get("sha256")

        if not target_version or not url or not sha256:
            _LOGGER.error(
                "NUBLY HA: firmware manifest incomplete (version=%s, url=%s, sha256=%s) — not sending update",
                bool(target_version),
                bool(url),
                bool(sha256),
            )
            return

        topic = f"nubly/devices/{self._device_id}/command/update"
        payload = {
            "version": target_version,
            "url": url,
            "sha256": sha256,
        }
        _LOGGER.info(
            "NUBLY HA: sending firmware update command to %s (version=%s)",
            topic,
            target_version,
        )
        try:
            await self.hass.services.async_call(
                "mqtt",
                "publish",
                {
                    "topic": topic,
                    "payload": json.dumps(payload),
                    "qos": 0,
                    "retain": False,
                },
                blocking=True,
            )
        except Exception:
            _LOGGER.exception(
                "NUBLY HA: failed to publish firmware update command"
            )
