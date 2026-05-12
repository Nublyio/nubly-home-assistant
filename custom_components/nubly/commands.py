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

        # Scene-button activations have their own payload shape
        # ({button_id, label, target_entity}) and dispatch to one of several
        # services depending on the target entity's domain.
        if command == "scene/activate":
            _handle_scene_activate(hass, device_id, data)
            return

        # Playing a Sonos favorite needs an allow-list check against the
        # device's own published favorites before we issue the service call.
        if command == "media/play_favorite":
            _handle_play_favorite(hass, device_id, data)
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


# Map of target entity domain -> (HA service domain, HA service name) used
# when a scene button is activated.
_SCENE_TARGET_SERVICES: dict[str, tuple[str, str]] = {
    "scene": ("scene", "turn_on"),
    "script": ("script", "turn_on"),
    "button": ("button", "press"),
    # Sensible fall-throughs for related domains, in case the user binds a
    # different actor to a scene button.
    "input_button": ("input_button", "press"),
    "automation": ("automation", "trigger"),
}


@callback
def _handle_scene_activate(
    hass: HomeAssistant, device_id: str, data: dict
) -> None:
    """Dispatch a `commands/scene/activate` payload to the right HA service."""
    button_id = (data.get("button_id") or "").strip()
    label = (data.get("label") or "").strip()
    target = (data.get("target_entity") or data.get("entity_id") or "").strip()

    _LOGGER.info(
        "NUBLY HA: scene activate received device=%s button_id=%s label=%r "
        "target_entity=%s",
        device_id,
        button_id or "<none>",
        label or "<none>",
        target or "<none>",
    )

    if not target or "." not in target:
        _LOGGER.warning(
            "NUBLY HA: scene activate missing/invalid target_entity device=%s "
            "button_id=%s",
            device_id,
            button_id or "<none>",
        )
        return

    domain = target.split(".", 1)[0]
    svc = _SCENE_TARGET_SERVICES.get(domain)
    if svc is None:
        _LOGGER.warning(
            "NUBLY HA: scene activate unsupported domain=%s target=%s "
            "device=%s button_id=%s",
            domain,
            target,
            device_id,
            button_id or "<none>",
        )
        return

    svc_domain, svc_name = svc
    _LOGGER.info(
        "NUBLY HA: scene activate calling %s.%s target=%s button_id=%s",
        svc_domain,
        svc_name,
        target,
        button_id or "<none>",
    )

    hass.async_create_task(
        _async_call_scene_service(
            hass, svc_domain, svc_name, target, button_id
        )
    )


@callback
def _handle_play_favorite(
    hass: HomeAssistant, device_id: str, data: dict
) -> None:
    """Dispatch a `commands/media/play_favorite` payload to media_player.play_media.

    Validates that the requested favorite is in the published list for
    this specific device before issuing the service call. This prevents
    a rogue MQTT producer from triggering arbitrary `play_media` calls
    against arbitrary entities.
    """
    from .const import CONF_DEVICE_ID, DOMAIN as NUBLY_DOMAIN

    media_content_id = (data.get("media_content_id") or "").strip()
    media_content_type = (
        data.get("media_content_type") or "favorite_item_id"
    ).strip()
    requested_entity = (data.get("entity_id") or "").strip()
    title = (data.get("title") or "").strip()

    _LOGGER.info(
        "NUBLY HA: play_favorite received device=%s entity=%s content_id=%s "
        "content_type=%s title=%r",
        device_id,
        requested_entity or "<none>",
        media_content_id or "<none>",
        media_content_type,
        title,
    )

    if not media_content_id:
        _LOGGER.warning(
            "NUBLY HA: play_favorite missing media_content_id device=%s",
            device_id,
        )
        return

    # Locate the device's structured config to validate the favorite.
    structured: dict | None = None
    for bucket in (hass.data.get(NUBLY_DOMAIN) or {}).values():
        if not isinstance(bucket, dict):
            continue
        cfg = bucket.get("config") or {}
        if cfg.get(CONF_DEVICE_ID) != device_id:
            continue
        candidate = bucket.get("structured")
        if isinstance(candidate, dict):
            structured = candidate
            break

    if structured is None:
        _LOGGER.warning(
            "NUBLY HA: play_favorite no structured config found device=%s",
            device_id,
        )
        return

    media_cfg = (structured.get("screens") or {}).get("media") or {}
    configured_entity = (media_cfg.get("entity_id") or "").strip()
    if not configured_entity:
        _LOGGER.warning(
            "NUBLY HA: play_favorite no media_player configured device=%s",
            device_id,
        )
        return

    # The device should target its own configured media_player. Anything
    # else is treated as a programming error and rejected.
    if requested_entity and requested_entity != configured_entity:
        _LOGGER.warning(
            "NUBLY HA: play_favorite entity mismatch device=%s requested=%s "
            "configured=%s",
            device_id,
            requested_entity,
            configured_entity,
        )
        return

    # The favorite must be one we actually published to this device. We
    # resolve the published list lazily by re-reading the favorites
    # source so the user doesn't have to round-trip a save when their
    # Sonos library changes.
    favorites = _published_favorites_for_validation(hass, media_cfg)
    allowed_ids = {f["media_content_id"] for f in favorites}
    if media_content_id not in allowed_ids:
        _LOGGER.warning(
            "NUBLY HA: play_favorite media_content_id not in allow-list "
            "device=%s content_id=%s allowed=%d",
            device_id,
            media_content_id,
            len(allowed_ids),
        )
        return

    _LOGGER.info(
        "NUBLY HA: play_favorite calling media_player.play_media entity=%s "
        "content_id=%s content_type=%s",
        configured_entity,
        media_content_id,
        media_content_type,
    )
    hass.async_create_task(
        _async_call_play_media(
            hass,
            configured_entity,
            media_content_id,
            media_content_type,
        )
    )


def _published_favorites_for_validation(
    hass: HomeAssistant, media_cfg: dict
) -> list[dict]:
    """Re-resolve the favorites list with the same logic as the publisher."""
    # Local import to avoid an import cycle with __init__.py.
    from . import _resolve_media_favorites  # type: ignore[attr-defined]

    return _resolve_media_favorites(hass, "<validation>", media_cfg)


async def _async_call_play_media(
    hass: HomeAssistant,
    entity_id: str,
    media_content_id: str,
    media_content_type: str,
) -> None:
    try:
        await hass.services.async_call(
            "media_player",
            "play_media",
            {
                "entity_id": entity_id,
                "media_content_id": media_content_id,
                "media_content_type": media_content_type,
            },
            blocking=False,
        )
    except Exception:
        _LOGGER.exception(
            "NUBLY HA: play_favorite media_player.play_media failed "
            "entity=%s content_id=%s",
            entity_id,
            media_content_id,
        )
        return
    _LOGGER.info(
        "NUBLY HA: play_favorite ok entity=%s content_id=%s",
        entity_id,
        media_content_id,
    )


async def _async_call_scene_service(
    hass: HomeAssistant,
    svc_domain: str,
    svc_name: str,
    target: str,
    button_id: str,
) -> None:
    try:
        await hass.services.async_call(
            svc_domain,
            svc_name,
            {"entity_id": target},
            blocking=False,
        )
    except Exception:
        _LOGGER.exception(
            "NUBLY HA: scene activate %s.%s failed for target=%s button_id=%s",
            svc_domain,
            svc_name,
            target,
            button_id or "<none>",
        )
        return
    _LOGGER.info(
        "NUBLY HA: scene activate ok %s.%s target=%s button_id=%s",
        svc_domain,
        svc_name,
        target,
        button_id or "<none>",
    )
