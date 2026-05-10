"""Nubly per-device diagnostic sensors."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, SIGNAL_STRENGTH_DECIBELS_MILLIWATT
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
            NublyFirmwareVersionSensor(device_data),
            NublyBoardSensor(device_data),
            NublyIPAddressSensor(device_data),
            NublyRSSISensor(device_data),
            NublyUptimeSensor(device_data),
            NublyActiveScreenSensor(device_data),
        ]
    )


class _NublyDiagnosticBase(SensorEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, data: NublyDeviceData, key: str, name: str) -> None:
        self._data = data
        self._attr_name = name
        self._attr_unique_id = f"{data.device_id}_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._data.device_id)})

    @property
    def available(self) -> bool:
        return self._data.available

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._data.add_listener(self.async_write_ha_state))


class NublyFirmwareVersionSensor(_NublyDiagnosticBase):
    def __init__(self, data: NublyDeviceData) -> None:
        super().__init__(data, "firmware_version", "Firmware version")

    @property
    def native_value(self):
        return get_attr(
            self._data.attributes, "firmware_version", "sw_version", "version"
        )


class NublyBoardSensor(_NublyDiagnosticBase):
    def __init__(self, data: NublyDeviceData) -> None:
        super().__init__(data, "board", "Board")

    @property
    def native_value(self):
        return get_attr(
            self._data.attributes, "board", "device_type", "model"
        )


class NublyIPAddressSensor(_NublyDiagnosticBase):
    _attr_icon = "mdi:ip-network"

    def __init__(self, data: NublyDeviceData) -> None:
        super().__init__(data, "ip_address", "IP address")

    @property
    def native_value(self):
        return get_attr(self._data.attributes, "ip_address", "ip")


class NublyRSSISensor(_NublyDiagnosticBase):
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT

    def __init__(self, data: NublyDeviceData) -> None:
        super().__init__(data, "rssi", "RSSI")

    @property
    def native_value(self):
        value = get_attr(self._data.attributes, "rssi", "wifi_rssi")
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None


class NublyUptimeSensor(_NublyDiagnosticBase):
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, data: NublyDeviceData) -> None:
        super().__init__(data, "uptime", "Uptime")
        self._last_seconds: int | None = None
        self._last_dt: datetime | None = None

    @property
    def native_value(self):
        seconds = get_attr(
            self._data.attributes, "uptime_seconds", "uptime"
        )
        if seconds is None:
            return self._last_dt
        try:
            seconds_int = int(seconds)
        except (TypeError, ValueError):
            return self._last_dt
        if seconds_int == self._last_seconds and self._last_dt is not None:
            return self._last_dt
        self._last_seconds = seconds_int
        self._last_dt = datetime.now(timezone.utc) - timedelta(
            seconds=seconds_int
        )
        return self._last_dt


class NublyActiveScreenSensor(_NublyDiagnosticBase):
    _attr_icon = "mdi:monitor-dashboard"

    def __init__(self, data: NublyDeviceData) -> None:
        super().__init__(data, "active_screen", "Active screen")

    @property
    def native_value(self):
        return get_attr(
            self._data.attributes, "active_screen", "current_screen", "screen"
        )
