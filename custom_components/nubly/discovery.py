"""MQTT-based discovery of Nubly devices."""

import asyncio
import json
import logging

from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant, callback

from .const import DISCOVERY_SUB_TOPIC, DISCOVERY_TIMEOUT

_LOGGER = logging.getLogger(__name__)


async def async_discover_devices(hass: HomeAssistant) -> set[str]:
    """Listen briefly on MQTT and return the set of Nubly device_ids seen.

    Reads `device_id` out of the JSON payload published to
    `nubly/devices/+/attributes` rather than relying on the topic segment,
    since the firmware treats the attributes payload as source of truth.
    """
    found: set[str] = set()

    @callback
    def on_message(msg) -> None:
        payload = msg.payload
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", errors="replace")

        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            _LOGGER.debug(
                "NUBLY HA: non-JSON attributes payload on %s", msg.topic
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

    try:
        unsub = await mqtt.async_subscribe(hass, DISCOVERY_SUB_TOPIC, on_message)
    except Exception:
        _LOGGER.exception("NUBLY HA: MQTT discovery subscribe failed")
        return found

    try:
        await asyncio.sleep(DISCOVERY_TIMEOUT)
    finally:
        unsub()

    return found
