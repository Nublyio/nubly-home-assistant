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

        # Lazy browse: the device asks for children of a category. We call
        # media_player.async_browse_media and publish the children back on
        # a per-device response topic.
        if command == "media/browse":
            hass.async_create_task(
                _handle_media_browse(hass, device_id, data)
            )
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

    # The favorite must be one we actually published to this device.
    # The publisher caches the allow-list synchronously after each
    # `_publish_config`, so the receive path stays non-blocking.
    allowed_ids: set = set()
    for bucket in (hass.data.get(NUBLY_DOMAIN) or {}).values():
        if not isinstance(bucket, dict):
            continue
        cfg = bucket.get("config") or {}
        if cfg.get(CONF_DEVICE_ID) == device_id:
            allowed_ids = bucket.get("favorite_ids") or set()
            break
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


async def _handle_media_browse(
    hass: HomeAssistant, device_id: str, data: dict
) -> None:
    """Browse a media_player category and publish children on the response topic.

    Request payload:
      {
        "request_id":         "abc123",         # optional, echoed in response
        "media_content_id":   "FV:2/31",
        "media_content_type": "..."             # optional
      }

    Response topic: nubly/devices/<device_id>/media/browse_response
    Response payload:
      {
        "request_id":       "abc123",
        "media_content_id": "FV:2/31",
        "items": [
          {"id":"item_1","title":"...","media_content_id":"...",
           "media_content_type":"...","playable":bool,"expandable":bool,
           "icon":"..."},
          ...
        ]
      }

    On error, `items` is empty and `error` carries a short reason string.
    """
    from .const import CONF_DEVICE_ID, DOMAIN as NUBLY_DOMAIN
    from . import _browse_child_to_runtime  # type: ignore[attr-defined]

    request_id = (data.get("request_id") or "").strip()
    media_content_id = (data.get("media_content_id") or "").strip()
    media_content_type = (data.get("media_content_type") or "").strip() or None

    _LOGGER.info(
        "NUBLY HA: media browse request device=%s request_id=%s "
        "content_id=%s content_type=%s",
        device_id,
        request_id or "<none>",
        media_content_id or "<none>",
        media_content_type or "<none>",
    )

    response_topic = f"nubly/devices/{device_id}/media/browse_response"

    if not media_content_id:
        await _publish_browse_response(
            hass, response_topic, request_id, "", [], error="missing_content_id"
        )
        return

    # Find the device's configured media_player and its bucket.
    configured_entity: str | None = None
    bucket_ref: dict | None = None
    for bucket in (hass.data.get(NUBLY_DOMAIN) or {}).values():
        if not isinstance(bucket, dict):
            continue
        cfg = bucket.get("config") or {}
        if cfg.get(CONF_DEVICE_ID) != device_id:
            continue
        structured = bucket.get("structured") or {}
        media_cfg = (structured.get("screens") or {}).get("media") or {}
        configured_entity = (media_cfg.get("entity_id") or "").strip() or None
        bucket_ref = bucket
        break

    if not configured_entity:
        _LOGGER.warning(
            "NUBLY HA: media browse — no media_player configured for device=%s",
            device_id,
        )
        await _publish_browse_response(
            hass,
            response_topic,
            request_id,
            media_content_id,
            [],
            error="no_media_entity",
        )
        return

    component = hass.data.get("media_player")
    if component is None:
        await _publish_browse_response(
            hass,
            response_topic,
            request_id,
            media_content_id,
            [],
            error="media_player_unavailable",
        )
        return
    player = component.get_entity(configured_entity)
    if player is None:
        await _publish_browse_response(
            hass,
            response_topic,
            request_id,
            media_content_id,
            [],
            error="entity_not_found",
        )
        return

    try:
        node = await player.async_browse_media(
            media_content_type, media_content_id
        )
    except Exception as err:
        _LOGGER.warning(
            "NUBLY HA: media browse raised device=%s content_id=%s: %s",
            device_id,
            media_content_id,
            err,
        )
        await _publish_browse_response(
            hass,
            response_topic,
            request_id,
            media_content_id,
            [],
            error="browse_failed",
        )
        return

    items: list[dict] = []
    new_ids: set[str] = set()
    for idx, child in enumerate(
        getattr(node, "children", None) or [], start=1
    ):
        entry = _browse_child_to_runtime(child)
        if entry is None:
            continue
        entry["id"] = f"item_{idx}"
        items.append(entry)
        new_ids.add(entry["media_content_id"])

    if not items:
        _LOGGER.info(
            "NUBLY HA: media browse empty device=%s content_id=%s "
            "(category has no children or is not browsable)",
            device_id,
            media_content_id,
        )

    # Extend the play_favorite allow-list with the newly seen content_ids
    # so the device can immediately play any of these without a republish.
    if bucket_ref is not None and new_ids:
        existing = bucket_ref.get("favorite_ids") or set()
        if isinstance(existing, set):
            bucket_ref["favorite_ids"] = existing | new_ids
        else:
            bucket_ref["favorite_ids"] = set(existing) | new_ids

    _LOGGER.info(
        "NUBLY HA: media browse response device=%s content_id=%s children=%d",
        device_id,
        media_content_id,
        len(items),
    )
    await _publish_browse_response(
        hass, response_topic, request_id, media_content_id, items
    )


async def _publish_browse_response(
    hass: HomeAssistant,
    topic: str,
    request_id: str,
    media_content_id: str,
    items: list[dict],
    *,
    error: str | None = None,
) -> None:
    """Publish a per-request browse response. Not retained — request-scoped."""
    payload: dict = {
        "request_id": request_id,
        "media_content_id": media_content_id,
        "items": items,
    }
    if error:
        payload["error"] = error
    try:
        await hass.services.async_call(
            "mqtt",
            "publish",
            {
                "topic": topic,
                "payload": json.dumps(payload),
                "qos": 0,
                "retain": False,
            },
            blocking=False,
        )
    except Exception:
        _LOGGER.exception(
            "NUBLY HA: media browse response publish failed topic=%s",
            topic,
        )


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
