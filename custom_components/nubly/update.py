"""Nubly firmware UpdateEntity.

One Update entity per Nubly device. Reads:
  - installed_version from device telemetry (attributes topic)
  - latest_version   from the firmware provider (NublyFirmwareProvider)
  - ota progress     from device_data (ota/state topic + attributes fallback)

OTA install is triggered by publishing to:
  nubly/devices/<device_id>/ota/install   {"version": "...", "url": "..."}

Compatibility: refuses to install if the resolved firmware's board doesn't
match the device's reported board.
"""

from __future__ import annotations

import json
import logging

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEVICE_ID, CONF_MODEL, CONF_SW_VERSION, DOMAIN
from .device_data import (
    NublyDeviceData,
    get_attr,
    ota_in_progress,
    ota_last_error,
    ota_last_result,
    ota_progress_percent,
)
from .firmware import (
    FirmwareInfo,
    NublyFirmwareProvider,
    SUPPORTED_BOARDS,
    normalize_board,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Nubly firmware Update entity for this config entry."""
    provider: NublyFirmwareProvider | None = hass.data[DOMAIN].get(
        "_firmware_provider"
    )
    if provider is None:
        provider = NublyFirmwareProvider(hass)
        hass.data[DOMAIN]["_firmware_provider"] = provider
        await provider.async_config_entry_first_refresh()

    device_data: NublyDeviceData = hass.data[DOMAIN][entry.entry_id][
        "device_data"
    ]
    async_add_entities(
        [NublyFirmwareUpdate(hass, entry, provider, device_data)]
    )


class NublyFirmwareUpdate(
    CoordinatorEntity[NublyFirmwareProvider], UpdateEntity
):
    """One firmware UpdateEntity per Nubly device."""

    _attr_supported_features = (
        UpdateEntityFeature.INSTALL
        | UpdateEntityFeature.PROGRESS
        | UpdateEntityFeature.RELEASE_NOTES
    )
    _attr_has_entity_name = True
    _attr_name = "Firmware"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        provider: NublyFirmwareProvider,
        device_data: NublyDeviceData,
    ) -> None:
        super().__init__(provider)
        self.hass = hass
        self._entry = entry
        self._device_data = device_data
        self._device_id = entry.data[CONF_DEVICE_ID]
        self._attr_unique_id = f"{self._device_id}_firmware"
        self._installed_version: str | None = entry.data.get(CONF_SW_VERSION)

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._device_id)})

    @property
    def available(self) -> bool:
        return bool(self._device_data.available) and self.coordinator.last_update_success

    @property
    def installed_version(self) -> str | None:
        device_version = get_attr(
            self._device_data.attributes,
            "firmware_version",
            "sw_version",
            "version",
        )
        if isinstance(device_version, str) and device_version:
            return device_version
        return self._installed_version

    @property
    def latest_version(self) -> str | None:
        info = self._resolve_firmware()
        return info.version if info else None

    @property
    def release_url(self) -> str | None:
        info = self._resolve_firmware()
        return info.release_url if info else None

    @property
    def in_progress(self) -> bool | int:
        if not ota_in_progress(self._device_data):
            return False
        pct = ota_progress_percent(self._device_data)
        return pct if pct is not None else True

    @property
    def update_percentage(self) -> int | None:
        if not ota_in_progress(self._device_data):
            return None
        return ota_progress_percent(self._device_data)

    @property
    def extra_state_attributes(self) -> dict:
        attrs: dict = {}
        board = self._device_board()
        if board:
            attrs["board"] = board
        last_result = ota_last_result(self._device_data)
        if last_result:
            attrs["ota_last_result"] = last_result
        last_error = ota_last_error(self._device_data)
        if last_error:
            attrs["ota_last_error"] = last_error
        return attrs

    async def async_release_notes(self) -> str | None:
        info = self._resolve_firmware()
        return info.release_notes if info else None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self._device_data.add_listener(self._handle_device_update)
        )

    def _handle_device_update(self) -> None:
        new_version = get_attr(
            self._device_data.attributes,
            "firmware_version",
            "sw_version",
            "version",
        )
        if (
            isinstance(new_version, str)
            and new_version
            and new_version != self._installed_version
        ):
            self._installed_version = new_version

        last_result = ota_last_result(self._device_data)
        last_error = ota_last_error(self._device_data)
        if last_result == "success":
            _LOGGER.debug(
                "NUBLY HA: OTA completed for %s (version=%s)",
                self._device_id,
                self.installed_version,
            )
        elif last_result and last_result != "success":
            _LOGGER.debug(
                "NUBLY HA: OTA failed for %s (result=%s, error=%s)",
                self._device_id,
                last_result,
                last_error,
            )

        self.async_write_ha_state()

    async def async_install(
        self, version: str | None, backup: bool, **kwargs
    ) -> None:
        """Publish the OTA install command for this device over MQTT."""
        _LOGGER.debug(
            "NUBLY HA: OTA install requested for %s (version=%s)",
            self._device_id,
            version,
        )

        if not self._device_data.available:
            _LOGGER.error(
                "NUBLY HA: device %s is offline — refusing OTA install",
                self._device_id,
            )
            return

        info = self._resolve_firmware()
        if info is None or not info.version or not info.firmware_url:
            _LOGGER.error(
                "NUBLY HA: firmware metadata unavailable for board=%s — "
                "refusing OTA install",
                self._device_board(),
            )
            return

        device_board = self._device_board()
        if (
            info.board
            and device_board
            and info.board in SUPPORTED_BOARDS
            and info.board != device_board
        ):
            _LOGGER.error(
                "NUBLY HA: firmware board=%s incompatible with device board=%s — "
                "refusing OTA install for %s",
                info.board,
                device_board,
                self._device_id,
            )
            return

        target_version = version or info.version
        topic = f"nubly/devices/{self._device_id}/ota/install"
        payload = {"version": target_version, "url": info.firmware_url}
        if info.sha256:
            payload["sha256"] = info.sha256

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
            _LOGGER.debug(
                "NUBLY HA: MQTT OTA command published to %s (version=%s)",
                topic,
                target_version,
            )
        except Exception:
            _LOGGER.exception(
                "NUBLY HA: failed to publish OTA install command for %s",
                self._device_id,
            )

    def _device_board(self) -> str | None:
        raw = get_attr(
            self._device_data.attributes, "board", "device_type", "model"
        )
        if isinstance(raw, str) and raw:
            return normalize_board(raw)
        # Fall back to whatever was captured at config-entry creation.
        configured = self._entry.data.get(CONF_MODEL)
        return normalize_board(configured) if isinstance(configured, str) else None

    def _resolve_firmware(self) -> FirmwareInfo | None:
        return self.coordinator.resolve(self._device_board())
