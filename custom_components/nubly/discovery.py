"""MQTT-based discovery of Nubly devices."""

import asyncio
import json
import logging

from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant, callback

from .const import DISCOVERY_SUB_TOPIC, DISCOVERY_TIMEOUT

_LOGGER = logging.getLogger(__name__)


async def async_discover_devices(hass: HomeAssistant) -> set[str]:
    """Listen on MQTT and return the set of Nubly device_ids seen.

    Subscribes to `nubly/devices/+/attributes` and extracts `device_id` from
    the JSON payload. Returns as soon as the first device is seen; otherwise
    waits up to DISCOVERY_TIMEOUT seconds.
    """
    found: set[str] = set()
    first_seen = asyncio.Event()

    @callback
    def on_message(msg) -> None:
        _LOGGER.info("NUBLY HA: attributes received topic = %s", msg.topic)

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
            _LOGGER.info("NUBLY HA: discovered device_id = %s", device_id)
            found.add(device_id)
            first_seen.set()

    try:
        await mqtt.async_wait_for_mqtt_client(hass)
    except Exception:
        _LOGGER.exception("NUBLY HA: MQTT client not ready")
        return found

    try:
        unsub = await mqtt.async_subscribe(hass, DISCOVERY_SUB_TOPIC, on_message)
    except Exception:
        _LOGGER.exception("NUBLY HA: MQTT discovery subscribe failed")
        return found

    _LOGGER.info(
        "NUBLY HA: listening on %s for up to %.0fs",
        DISCOVERY_SUB_TOPIC,
        DISCOVERY_TIMEOUT,
    )

    try:
        try:
            await asyncio.wait_for(first_seen.wait(), timeout=DISCOVERY_TIMEOUT)
        except asyncio.TimeoutError:
            _LOGGER.warning("NUBLY HA: no device attributes received yet")
    finally:
        unsub()

    return found
