"""MQTT command handling for Nubly devices."""

import json
import logging

from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant, callback

_LOGGER = logging.getLogger(__name__)


# Maps a Nubly command suffix to (HA domain, HA service, allowed payload keys).
# "entity_id" is always required; other keys are forwarded if present.
_COMMAND_MAP: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "light/toggle": ("light", "toggle", ("entity_id",)),
    "light/brightness_set": ("light", "turn_on", ("entity_id", "brightness_pct")),
    "media/play_pause": ("media_player", "media_play_pause", ("entity_id",)),
    "media/next_track": ("media_player", "media_next_track", ("entity_id",)),
    "media/previous_track": ("media_player", "media_previous_track", ("entity_id",)),
    "media/seek": ("media_player", "media_seek", ("entity_id", "seek_position")),
    "media/volume_set": ("media_player", "volume_set", ("entity_id", "volume_level")),
}


async def async_subscribe_commands(hass: HomeAssistant, device_id: str):
    """Subscribe to nubly/devices/<device_id>/commands/# and dispatch to services.

    Returns the unsubscribe callable from mqtt.async_subscribe.
    """
    prefix = f"nubly/devices/{device_id}/commands/"
    wildcard = f"{prefix}#"

    @callback
    def on_message(msg) -> None:
        _LOGGER.debug("NUBLY HA: command received topic = %s", msg.topic)

        if not msg.topic.startswith(prefix):
            return
        command = msg.topic[len(prefix):]

        payload = msg.payload
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", errors="replace")

        try:
            data = json.loads(payload) if payload else {}
        except (json.JSONDecodeError, TypeError):
            _LOGGER.debug(
                "NUBLY HA: non-JSON command payload on %s", msg.topic
            )
            return

        if not isinstance(data, dict):
            _LOGGER.debug("NUBLY HA: command payload must be a JSON object")
            return

        spec = _COMMAND_MAP.get(command)
        if spec is None:
            _LOGGER.debug("NUBLY HA: unknown command %s", command)
            return

        domain, service, fields = spec
        service_data = {k: data[k] for k in fields if k in data}
        if "entity_id" not in service_data:
            _LOGGER.warning("NUBLY HA: missing entity_id for command %s", command)
            return

        if command == "media/previous_track":
            _LOGGER.info("NUBLY HA: media previous command received")
            _LOGGER.info(
                "NUBLY HA: calling media_player.media_previous_track for %s",
                service_data.get("entity_id"),
            )
        elif command == "media/seek":
            _LOGGER.info("NUBLY HA: media seek command received")
            _LOGGER.info(
                "NUBLY HA: calling media_player.media_seek position = %s",
                service_data.get("seek_position"),
            )
        else:
            _LOGGER.debug(
                "NUBLY HA: calling service = %s.%s", domain, service
            )

        hass.async_create_task(
            _async_call_service(hass, domain, service, service_data)
        )

    return await mqtt.async_subscribe(hass, wildcard, on_message)


async def _async_call_service(
    hass: HomeAssistant, domain: str, service: str, service_data: dict
) -> None:
    try:
        await hass.services.async_call(domain, service, service_data, blocking=False)
    except Exception:
        _LOGGER.exception(
            "NUBLY HA: command service call failed %s.%s", domain, service
        )
        return
    _LOGGER.debug("NUBLY HA: command handled ok")
