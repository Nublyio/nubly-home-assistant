"""Per-device runtime state shared by Nubly diagnostic entities.

Subscribes once per device to:
  nubly/devices/<device_id>/attributes
  nubly/devices/<device_id>/availability

Entities register listeners and read fields from `attributes` / `available`.
"""

from __future__ import annotations

import json
import logging
from typing import Callable

from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant, callback

_LOGGER = logging.getLogger(__name__)


class NublyDeviceData:
    """Cache of latest attributes + availability for a single Nubly device."""

    def __init__(self, hass: HomeAssistant, device_id: str) -> None:
        self.hass = hass
        self.device_id = device_id
        self.attributes: dict = {}
        self.ota_state: dict = {}
        self.available: bool = False
        self._listeners: list[Callable[[], None]] = []
        self._unsubs: list[Callable[[], None]] = []

    async def async_start(self) -> None:
        attrs_topic = f"nubly/devices/{self.device_id}/attributes"
        avail_topic = f"nubly/devices/{self.device_id}/availability"
        ota_topic = f"nubly/devices/{self.device_id}/ota/state"

        self._unsubs.append(
            await mqtt.async_subscribe(
                self.hass, attrs_topic, self._on_attributes
            )
        )
        self._unsubs.append(
            await mqtt.async_subscribe(
                self.hass, avail_topic, self._on_availability
            )
        )
        self._unsubs.append(
            await mqtt.async_subscribe(
                self.hass, ota_topic, self._on_ota_state
            )
        )

    @callback
    def async_stop(self) -> None:
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:
                _LOGGER.exception("NUBLY HA: unsubscribe failed")
        self._unsubs.clear()
        self._listeners.clear()

    @callback
    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)

        def _remove() -> None:
            try:
                self._listeners.remove(cb)
            except ValueError:
                pass

        return _remove

    @callback
    def _notify(self) -> None:
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:
                _LOGGER.exception("NUBLY HA: device listener raised")

    @callback
    def _on_attributes(self, msg) -> None:
        payload = msg.payload
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", errors="replace")
        if not payload:
            return
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, TypeError, ValueError):
            return
        if not isinstance(data, dict):
            return
        self.attributes = data
        self._notify()

    @callback
    def _on_ota_state(self, msg) -> None:
        topic = getattr(msg, "topic", f"nubly/devices/{self.device_id}/ota/state")
        payload = msg.payload
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", errors="replace")
        _LOGGER.debug(
            "NUBLY HA: OTA MQTT received topic=%s device=%s payload=%s",
            topic,
            self.device_id,
            payload,
        )
        if not payload:
            self.ota_state = {}
            self._notify()
            return
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, TypeError, ValueError):
            _LOGGER.debug(
                "NUBLY HA: OTA state payload not JSON for %s: %s",
                self.device_id,
                payload,
            )
            return
        if not isinstance(data, dict):
            return
        self.ota_state = data
        _LOGGER.debug(
            "NUBLY HA: OTA state parsed for %s state=%s progress=%s",
            self.device_id,
            data.get("state"),
            data.get("progress"),
        )
        self._notify()

    @callback
    def _on_availability(self, msg) -> None:
        payload = msg.payload
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", errors="replace")
        text = (payload or "").strip().lower()
        new_available = text in ("online", "true", "1", "available")
        if text in ("offline", "false", "0", "unavailable"):
            new_available = False
        if new_available != self.available:
            self.available = new_available
            self._notify()


# Firmware OTA state machine — the device reports one of these strings on
# nubly/devices/<id>/ota/state.
OTA_ACTIVE_STATES: frozenset[str] = frozenset(
    {"received", "validating", "connecting", "downloading", "writing"}
)
OTA_TERMINAL_STATES: frozenset[str] = frozenset({"idle", "success", "failed"})


def ota_state_name(data: "NublyDeviceData") -> str | None:
    """Return the current OTA state string (lowercased), or None."""
    raw = None
    if isinstance(data.ota_state, dict):
        raw = data.ota_state.get("state") or data.ota_state.get("ota_state")
    if raw is None and isinstance(data.attributes, dict):
        raw = data.attributes.get("ota_state")
    if isinstance(raw, str) and raw:
        return raw.strip().lower()
    return None


def _ota_field(data: "NublyDeviceData", *keys: str):
    """Read an OTA field from ota_state first, then attributes."""
    for source in (data.ota_state, data.attributes):
        if not isinstance(source, dict):
            continue
        for key in keys:
            if key in source:
                value = source[key]
                if value not in (None, ""):
                    return value
    return None


def ota_in_progress(data: "NublyDeviceData") -> bool | None:
    """Whether an OTA install is currently active.

    Drives Update.in_progress (must be bool | None, never numeric).
    Prefers the explicit `state` field; falls back to legacy boolean fields.
    """
    state = ota_state_name(data)
    if state in OTA_ACTIVE_STATES:
        return True
    if state in OTA_TERMINAL_STATES:
        return False

    value = _ota_field(data, "ota_in_progress", "in_progress", "ota_active")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    if isinstance(value, (int, float)):
        return bool(value)
    return None


def ota_progress_percent(data: "NublyDeviceData") -> int | None:
    """Numeric progress (0-100) only while an OTA is active; else None."""
    if ota_in_progress(data) is not True:
        return None
    value = _ota_field(data, "progress", "ota_progress", "ota_percent")
    if value is None:
        return None
    try:
        pct = int(float(value))
    except (TypeError, ValueError):
        return None
    return max(0, min(100, pct))


def ota_last_result(data: "NublyDeviceData") -> str | None:
    value = _ota_field(data, "ota_last_result", "last_result", "result")
    return value if isinstance(value, str) else None


def ota_last_error(data: "NublyDeviceData") -> str | None:
    value = _ota_field(data, "ota_last_error", "last_error", "error")
    return value if isinstance(value, str) else None


def get_attr(attrs: dict, *keys: str):
    """Return the first non-empty value among the given keys."""
    for key in keys:
        if key in attrs:
            value = attrs[key]
            if value not in (None, ""):
                return value
    return None
