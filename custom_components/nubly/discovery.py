"""MQTT-based discovery of Nubly devices."""

import asyncio
import json
import logging

from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant, callback

from .const import DISCOVERY_SUB_TOPIC, DISCOVERY_TIMEOUT

_LOGGER = logging.getLogger(__name__)

# Max seconds to block waiting for the MQTT client to report "connected".
# If this expires we continue anyway — async_subscribe queues internally.
_MQTT_READY_TIMEOUT = 10.0


async def async_discover_devices(hass: HomeAssistant) -> set[str]:
    """Listen on MQTT and return the set of Nubly device_ids seen."""
    found: set[str] = set()
    first_seen = asyncio.Event()

    @callback
    def on_message(msg) -> None:
        _LOGGER.warning("NUBLY HA: attributes received topic = %s", msg.topic)

        payload = msg.payload
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", errors="replace")

        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            _LOGGER.warning(
                "NUBLY HA: non-JSON attributes payload on %s: %r",
                msg.topic,
                payload,
            )
            return

        device_id = data.get("device_id") if isinstance(data, dict) else None
        if (
            isinstance(device_id, str)
            and device_id.startswith("nubly_")
            and device_id not in found
        ):
            _LOGGER.warning("NUBLY HA: discovered device_id = %s", device_id)
            found.add(device_id)
            first_seen.set()

    _LOGGER.warning("NUBLY HA: before async_wait_for_mqtt_client")
    wait_fn = getattr(mqtt, "async_wait_for_mqtt_client", None)
    if wait_fn is None:
        _LOGGER.warning(
            "NUBLY HA: async_wait_for_mqtt_client not available in this HA version, skipping"
        )
    else:
        try:
            await asyncio.wait_for(wait_fn(hass), timeout=_MQTT_READY_TIMEOUT)
            _LOGGER.warning("NUBLY HA: after async_wait_for_mqtt_client (ready)")
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "NUBLY HA: async_wait_for_mqtt_client timed out after %.0fs, continuing anyway",
                _MQTT_READY_TIMEOUT,
            )
        except Exception:
            _LOGGER.exception(
                "NUBLY HA: async_wait_for_mqtt_client raised, continuing anyway"
            )

    _LOGGER.warning("NUBLY HA: before subscribe %s", DISCOVERY_SUB_TOPIC)
    try:
        unsub = await mqtt.async_subscribe(hass, DISCOVERY_SUB_TOPIC, on_message)
    except Exception:
        _LOGGER.exception("NUBLY HA: MQTT discovery subscribe failed")
        return found
    _LOGGER.warning("NUBLY HA: after subscribe (listening up to %.0fs)", DISCOVERY_TIMEOUT)

    _LOGGER.warning("NUBLY HA: before waiting for attributes")
    try:
        try:
            await asyncio.wait_for(first_seen.wait(), timeout=DISCOVERY_TIMEOUT)
        except asyncio.TimeoutError:
            _LOGGER.warning("NUBLY HA: no device attributes received yet")
    finally:
        unsub()
    _LOGGER.warning("NUBLY HA: after waiting for attributes (found=%s)", found)

    return found
