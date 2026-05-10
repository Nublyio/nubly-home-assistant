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
        self.available: bool = False
        self._listeners: list[Callable[[], None]] = []
        self._unsubs: list[Callable[[], None]] = []

    async def async_start(self) -> None:
        attrs_topic = f"nubly/devices/{self.device_id}/attributes"
        avail_topic = f"nubly/devices/{self.device_id}/availability"

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


def get_attr(attrs: dict, *keys: str):
    """Return the first non-empty value among the given keys."""
    for key in keys:
        if key in attrs:
            value = attrs[key]
            if value not in (None, ""):
                return value
    return None
