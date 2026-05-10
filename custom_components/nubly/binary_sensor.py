"""Nubly per-device binary diagnostic sensors."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .device_data import NublyDeviceData, get_attr


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    device_data: NublyDeviceData = hass.data[DOMAIN][entry.entry_id]["device_data"]
    async_add_entities(
        [
            NublyOnlineBinarySensor(device_data),
            NublyScreensaverBinarySensor(device_data),
        ]
    )


class _NublyBinaryBase(BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, data: NublyDeviceData, key: str, name: str) -> None:
        self._data = data
        self._attr_name = name
        self._attr_unique_id = f"{data.device_id}_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._data.device_id)})

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._data.add_listener(self.async_write_ha_state))


class NublyOnlineBinarySensor(_NublyBinaryBase):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, data: NublyDeviceData) -> None:
        super().__init__(data, "online", "Online")

    @property
    def is_on(self) -> bool:
        return self._data.available

    @property
    def available(self) -> bool:
        return True


class NublyScreensaverBinarySensor(_NublyBinaryBase):
    _attr_icon = "mdi:monitor-off"

    def __init__(self, data: NublyDeviceData) -> None:
        super().__init__(data, "screensaver_active", "Screensaver active")

    @property
    def is_on(self) -> bool | None:
        value = get_attr(
            self._data.attributes,
            "screensaver_active",
            "screensaver",
            "screensaver_on",
        )
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "on", "yes", "active")
        if isinstance(value, (int, float)):
            return bool(value)
        return None

    @property
    def available(self) -> bool:
        return self._data.available
