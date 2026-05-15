"""The Nubly integration."""

import asyncio
import json
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .const import (
    CONF_CONFIG,
    CONF_DEVICE_ID,
    CONF_MODEL,
    CONF_ROOM_NAME,
    CONF_SW_VERSION,
    DEFAULT_SCREENSAVER_TIMEOUT,
    DOMAIN,
    LEGACY_DEVICE_ID,
)
from .commands import async_subscribe_commands
from .device_data import NublyDeviceData
from .discovery import async_discover_devices
from .nubly_config import ensure_structured
from .provisioning import async_check_provisioning_support
from .view import NublyCoverArtView

_LOGGER = logging.getLogger(__name__)

_PUBLISH_MAX_ATTEMPTS = 5
_PUBLISH_RETRY_DELAY_SECONDS = 2

PLATFORMS = ["update", "sensor", "binary_sensor"]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the Nubly integration."""
    hass.data.setdefault(DOMAIN, {})
    _register_cover_art_view(hass)
    await _async_check_provisioning_once(hass)
    _register_services(hass)
    _log_board_registry()
    return True


def _log_board_registry() -> None:
    """Log every board type the integration knows about, once."""
    # Local import to avoid a startup-time cycle.
    from .firmware import BOARD_CAPABILITIES, SUPPORTED_BOARDS

    _LOGGER.info(
        "NUBLY HA: registered boards = %s",
        {b: BOARD_CAPABILITIES.get(b, {}) for b in sorted(SUPPORTED_BOARDS)},
    )


def _register_services(hass: HomeAssistant) -> None:
    """Register integration-level services.

    nubly.publish_config — manually republish the retained runtime config.
    Optional `device_id` filter; otherwise applies to every configured
    Nubly device.
    """
    flag_key = "_services_registered"
    if hass.data[DOMAIN].get(flag_key):
        return

    async def _service_publish_config(call) -> None:
        target = (call.data.get("device_id") or "").strip()
        published = 0
        for entry_id, bucket in list(hass.data[DOMAIN].items()):
            if not isinstance(bucket, dict):
                continue
            structured = bucket.get("structured")
            cfg = bucket.get("config") or {}
            device_id = cfg.get(CONF_DEVICE_ID)
            if not device_id or not isinstance(structured, dict):
                continue
            if target and target != device_id:
                continue
            _LOGGER.info(
                "NUBLY HA: nubly.publish_config service called device=%s",
                device_id,
            )
            await _publish_config(hass, device_id, structured)
            published += 1
        if target and published == 0:
            _LOGGER.warning(
                "NUBLY HA: nubly.publish_config target device_id=%s not found",
                target,
            )

    hass.services.async_register(DOMAIN, "publish_config", _service_publish_config)

    async def _service_debug_media_browser(call) -> None:
        """Diagnostic: dump async_browse_media result for a media_player.

        Temporary helper to figure out which media_content_id / type to use
        when navigating Sonos favorites. Logs the root node summary and up
        to the first 20 children with their full identifying fields.
        """
        entity_id = (call.data.get("entity_id") or "").strip()
        content_id = call.data.get("media_content_id")
        content_type = call.data.get("media_content_type")
        if not entity_id:
            _LOGGER.warning("NUBLY HA: debug_media_browser called without entity_id")
            return

        component = hass.data.get("media_player")
        if component is None:
            _LOGGER.warning(
                "NUBLY HA: debug_media_browser — media_player component not loaded"
            )
            return
        player = component.get_entity(entity_id)
        if player is None:
            _LOGGER.warning(
                "NUBLY HA: debug_media_browser — entity %s not found", entity_id
            )
            return

        _LOGGER.warning(
            "NUBLY HA: debug_media_browser START entity=%s content_id=%s content_type=%s",
            entity_id,
            content_id,
            content_type,
        )
        try:
            node = await player.async_browse_media(content_type, content_id)
        except Exception as err:
            _LOGGER.warning(
                "NUBLY HA: debug_media_browser raised entity=%s content_id=%s "
                "content_type=%s err=%r",
                entity_id,
                content_id,
                content_type,
                err,
            )
            return

        _dump_browse_node(entity_id, node)
        _LOGGER.warning("NUBLY HA: debug_media_browser END entity=%s", entity_id)

    hass.services.async_register(
        DOMAIN, "debug_media_browser", _service_debug_media_browser
    )
    hass.data[DOMAIN][flag_key] = True


def _dump_browse_node(entity_id: str, node) -> None:
    """Pretty-log a BrowseMedia node and its first 20 children."""
    children = list(getattr(node, "children", None) or [])
    _LOGGER.warning(
        "NUBLY HA: debug node entity=%s title=%r media_class=%s "
        "media_content_id=%s media_content_type=%s can_play=%s can_expand=%s "
        "children_count=%d",
        entity_id,
        getattr(node, "title", None),
        getattr(node, "media_class", None),
        getattr(node, "media_content_id", None),
        getattr(node, "media_content_type", None),
        getattr(node, "can_play", None),
        getattr(node, "can_expand", None),
        len(children),
    )
    for idx, child in enumerate(children[:20], start=1):
        _LOGGER.warning(
            "NUBLY HA: debug   child %02d title=%r media_class=%s "
            "media_content_id=%s media_content_type=%s can_play=%s can_expand=%s",
            idx,
            getattr(child, "title", None),
            getattr(child, "media_class", None),
            getattr(child, "media_content_id", None),
            getattr(child, "media_content_type", None),
            getattr(child, "can_play", None),
            getattr(child, "can_expand", None),
        )
    if len(children) > 20:
        _LOGGER.warning(
            "NUBLY HA: debug   ... and %d more children (not shown)",
            len(children) - 20,
        )


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate a v1 entry to v2 (hardware device_id)."""
    _LOGGER.info(
        "NUBLY HA: migrating entry %s from version %s",
        entry.entry_id,
        entry.version,
    )

    if entry.version < 2:
        new_data = dict(entry.data)

        if new_data.get(CONF_DEVICE_ID) == LEGACY_DEVICE_ID:
            _LOGGER.warning(
                "NUBLY HA: legacy device_id %s found, attempting discovery",
                LEGACY_DEVICE_ID,
            )
            found = await async_discover_devices(hass)
            if found:
                new_device_id = sorted(found)[0]
                _LOGGER.info(
                    "NUBLY HA: migrating device_id %s -> %s",
                    LEGACY_DEVICE_ID,
                    new_device_id,
                )
                new_data[CONF_DEVICE_ID] = new_device_id
            else:
                _LOGGER.warning(
                    "NUBLY HA: no Nubly devices responded; keeping legacy id"
                )

        hass.config_entries.async_update_entry(entry, data=new_data, version=2)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Nubly from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    _register_cover_art_view(hass)
    await _async_check_provisioning_once(hass)

    data = dict(entry.data)
    device_id = data.get(CONF_DEVICE_ID, "<unknown>")
    raw_model = data.get(CONF_MODEL)
    # Local import to avoid a cycle at module load.
    from .firmware import normalize_board as _norm_board

    normalized_board = _norm_board(raw_model) if isinstance(raw_model, str) else None
    _LOGGER.info(
        "NUBLY HA: integration setup started entry=%s device_id=%s "
        "model=%s board=%s sw_version=%s",
        entry.entry_id,
        device_id,
        raw_model,
        normalized_board,
        data.get(CONF_SW_VERSION),
    )

    try:
        if data.get(CONF_DEVICE_ID) == LEGACY_DEVICE_ID:
            _LOGGER.warning(
                "NUBLY HA: entry still uses legacy device_id %s, rediscovering",
                LEGACY_DEVICE_ID,
            )
            discovered = await async_discover_devices(hass)
            _LOGGER.debug("NUBLY HA: rediscovery returned %s", discovered)
            if discovered:
                new_device_id = sorted(discovered)[0]
                _LOGGER.info(
                    "NUBLY HA: updating device_id %s -> %s",
                    LEGACY_DEVICE_ID,
                    new_device_id,
                )
                data[CONF_DEVICE_ID] = new_device_id
                hass.config_entries.async_update_entry(
                    entry, data=data, unique_id=new_device_id
                )
                await _clear_legacy_config(hass)
            else:
                _LOGGER.warning(
                    "NUBLY HA: no Nubly devices responded; keeping legacy id"
                )
    except Exception:
        _LOGGER.exception("NUBLY HA: rediscovery block raised an exception")

    # Migrate any pre-v2 flat config into the structured shape and persist
    # it under entry.options[CONF_CONFIG]. Idempotent — runs only when no
    # structured config is present yet.
    structured, migrated = ensure_structured(data, dict(entry.options))
    if migrated:
        new_options = {**entry.options, CONF_CONFIG: structured}
        hass.config_entries.async_update_entry(entry, options=new_options)

    device_data = NublyDeviceData(hass, data[CONF_DEVICE_ID])
    try:
        await device_data.async_start()
    except Exception:
        _LOGGER.exception(
            "NUBLY HA: failed to subscribe to attributes/availability for %s",
            data[CONF_DEVICE_ID],
        )

    hass.data[DOMAIN][entry.entry_id] = {
        # Keep the legacy flat dict around for compatibility with code paths
        # that still read it (e.g. cover-art view). All publishing now uses
        # `structured`.
        "config": {**data, **{k: v for k, v in entry.options.items() if k != CONF_CONFIG}},
        "structured": structured,
        "device_data": device_data,
    }

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    # Republish the retained runtime config whenever the device transitions
    # offline → online. This handles device reboots, Wi-Fi blips, and any
    # case where the retained message was cleared before the device
    # subscribed.
    online_state = {"last": bool(device_data.available)}

    def _on_device_state_change() -> None:
        was_online = online_state["last"]
        now_online = bool(device_data.available)
        if now_online != was_online:
            online_state["last"] = now_online
            if now_online:
                _LOGGER.info(
                    "NUBLY HA: device came online device_id=%s — republishing runtime config",
                    device_data.device_id,
                )
                bucket = hass.data[DOMAIN].get(entry.entry_id)
                latest = (
                    bucket.get("structured")
                    if isinstance(bucket, dict)
                    else None
                )
                if isinstance(latest, dict):
                    hass.async_create_task(
                        _publish_config(hass, device_data.device_id, latest)
                    )

    entry.async_on_unload(device_data.add_listener(_on_device_state_change))

    device_id = data[CONF_DEVICE_ID]
    dev_reg = dr.async_get(hass)
    dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, device_id)},
        manufacturer="Nubly",
        name=structured["room"].get("name") or device_id,
        model=data.get(CONF_MODEL),
        sw_version=data.get(CONF_SW_VERSION),
    )

    try:
        unsub_commands = await async_subscribe_commands(hass, device_id)
        entry.async_on_unload(unsub_commands)
        _LOGGER.debug(
            "NUBLY HA: subscribed to commands for device_id = %s", device_id
        )
    except Exception:
        _LOGGER.exception("NUBLY HA: command subscribe failed")

    try:
        await _publish_config(hass, device_id, structured)
    except Exception:
        _LOGGER.exception("NUBLY HA: publish block raised an exception")

    try:
        await _clear_legacy_config(hass)
    except Exception:
        _LOGGER.exception("NUBLY HA: legacy cleanup raised an exception")

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _LOGGER.info(
        "NUBLY HA: integration setup completed device_id=%s board=%s "
        "room=%r topic=nubly/devices/%s/config",
        device_id,
        normalized_board,
        structured.get("room", {}).get("name"),
        device_id,
    )
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Republish retained MQTT config when options change.

    No Wi-Fi/MQTT provisioning, no Mosquitto restart, no /provision POST —
    only the HA config payload is updated.
    """
    data = dict(entry.data)
    structured, _ = ensure_structured(data, dict(entry.options))

    bucket = hass.data[DOMAIN].get(entry.entry_id)
    if isinstance(bucket, dict):
        bucket["structured"] = structured
        # Keep the legacy flat view fresh too — used by cover-art view.
        bucket["config"] = {
            **data,
            **{k: v for k, v in entry.options.items() if k != CONF_CONFIG},
        }

    device_id = data.get(CONF_DEVICE_ID)
    _LOGGER.info(
        "NUBLY HA: options saved device_id=%s republishing schema_version=%s",
        device_id,
        structured.get("schema_version"),
    )

    if device_id:
        dev_reg = dr.async_get(hass)
        device = dev_reg.async_get_device(identifiers={(DOMAIN, device_id)})
        if device is not None:
            new_name = structured["room"].get("name") or device_id
            if device.name != new_name:
                dev_reg.async_update_device(device.id, name=new_name)

    try:
        await _publish_config(hass, device_id, structured)
    except Exception:
        _LOGGER.exception("NUBLY HA: republish on options update failed")


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Nubly config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS
    )
    if unload_ok:
        bucket = hass.data[DOMAIN].pop(entry.entry_id, None)
        if isinstance(bucket, dict):
            device_data = bucket.get("device_data")
            if isinstance(device_data, NublyDeviceData):
                device_data.async_stop()
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Unprovision the device when its config entry is deleted."""
    device_id = entry.data.get(CONF_DEVICE_ID)
    _LOGGER.info(
        "NUBLY HA: removing config entry for device_id = %s", device_id
    )
    if not device_id:
        return

    config_topic = f"nubly/devices/{device_id}/config"
    _LOGGER.debug(
        "NUBLY HA: clearing retained config topic = %s", config_topic
    )
    try:
        await hass.services.async_call(
            "mqtt",
            "publish",
            {
                "topic": config_topic,
                "payload": "",
                "qos": 0,
                "retain": True,
            },
        )
    except Exception:
        _LOGGER.exception("NUBLY HA: failed to clear retained config")

    command_topic = f"nubly/devices/{device_id}/command/unprovision"
    try:
        await hass.services.async_call(
            "mqtt",
            "publish",
            {
                "topic": command_topic,
                "payload": "true",
                "qos": 0,
                "retain": False,
            },
        )
        _LOGGER.info("NUBLY HA: unprovision command sent")
    except Exception:
        _LOGGER.exception("NUBLY HA: failed to send unprovision command")


async def _publish_config(
    hass: HomeAssistant, device_id: str, structured: dict
) -> None:
    """Publish runtime device configuration to MQTT.

    Builds the firmware-compatible `room_controller` payload from the
    structured config and adds a top-level `schema_version` field so the
    device (and future firmware) can branch on shape changes.
    """
    # Guard: refuse to publish into a malformed topic when the entry
    # somehow lost its device_id. The previous fall-through would have
    # produced `nubly/devices//config` and silently failed on the device.
    if not isinstance(device_id, str) or not device_id.strip():
        _LOGGER.error(
            "NUBLY HA: refusing to publish runtime config — empty device_id "
            "(structured.room=%r)",
            structured.get("room"),
        )
        return

    room_meta = structured.get("room") or {}
    screens = structured.get("screens") or {}
    screensaver = structured.get("screensaver") or {}

    room_name = room_meta.get("name") or device_id
    room_id = room_meta.get("id") or device_id

    # Lights — translate {entity_id,label,icon} -> firmware {entity_id,name,type}.
    lights: list[dict] = []
    for entry in (screens.get("lights") or {}).get("entities") or []:
        if not isinstance(entry, dict):
            continue
        entity_id = (entry.get("entity_id") or "").strip()
        if not entity_id:
            continue
        lights.append(
            {
                "entity_id": entity_id,
                "name": (entry.get("label") or "").strip()
                or _entity_friendly_name(hass, entity_id),
                "type": "dimmer",
            }
        )

    scene_buttons = _scene_buttons_payload(structured.get("scene_buttons"))
    room: dict = {
        "id": room_id,
        "name": room_name,
        "lights": lights,
        # `scene_buttons` is the canonical path the firmware probes for.
        # `scenes` is kept as an alias for older firmware revisions.
        "scene_buttons": scene_buttons,
        "scenes": scene_buttons,
    }

    media = screens.get("media") or {}
    if media.get("entity_id"):
        room["media"] = {
            "entity_id": media["entity_id"],
            "cover_art_url": _build_cover_art_url(hass, device_id),
        }
        if media.get("label"):
            room["media"]["label"] = media["label"]

        if media.get("favorites_enabled"):
            favorites, children_map = await _resolve_media_favorites(
                hass, device_id, media
            )
            if favorites:
                room["media"]["favorites"] = favorites
            # Cache favorites + pre-traversed category children so the
            # browse / play_favorite handlers can resolve children
            # synchronously without a second live browse on demand.
            _stash_favorites_cache(hass, device_id, favorites, children_map)

    weather = screens.get("weather") or {}
    if weather.get("entity_id"):
        room["weather"] = {"entity_id": weather["entity_id"]}

    ambient = screens.get("ambient") or {}
    if ambient.get("temperature_entity") or ambient.get("humidity_entity"):
        room["ambient"] = {
            k: v
            for k, v in {
                "temperature_entity": ambient.get("temperature_entity"),
                "humidity_entity": ambient.get("humidity_entity"),
            }.items()
            if v
        }

    # Optional screen order — emitted only when configured so devices with
    # no preference fall back to their built-in default order.
    raw_order = screens.get("order") or []
    screen_order: list[str] = []
    seen_ids: set[str] = set()
    for sid in raw_order:
        if isinstance(sid, str):
            normalized = sid.strip().lower()
            if normalized and normalized not in seen_ids:
                seen_ids.add(normalized)
                screen_order.append(normalized)

    timeout = int(
        screensaver.get("timeout_seconds")
        or DEFAULT_SCREENSAVER_TIMEOUT
    )

    payload = {
        "schema_version": int(structured.get("schema_version") or 2),
        "mode": "room_controller",
        "device_id": device_id,
        "room": room,
        # Top-level alias so devices that probe at the root still find it.
        "scene_buttons": scene_buttons,
        "screensaver": {
            "enabled": bool(screensaver.get("enabled", True)),
            "type": screensaver.get("type") or "analog_clock",
            "timeout_seconds": timeout,
        },
        # Legacy top-level alias still consumed by current firmware until
        # the device branches on schema_version.
        "screensaver_timeout": timeout,
    }

    if screen_order:
        # Emit at room.screens.order (spec) and top-level screens.order so
        # any probe path the firmware uses finds the same value.
        room["screens"] = {"order": screen_order}
        payload["screens"] = {"order": screen_order}

    topic = f"nubly/devices/{device_id}/config"

    # Resolve the device board for the diagnostic log. Looks at the
    # per-entry bucket first (live model from attributes), then falls
    # back to whatever was captured at config-entry creation.
    board_for_log: str | None = None
    try:
        from .firmware import normalize_board as _norm

        for bucket in (hass.data.get(DOMAIN) or {}).values():
            if not isinstance(bucket, dict):
                continue
            cfg = bucket.get("config") or {}
            if cfg.get(CONF_DEVICE_ID) != device_id:
                continue
            device_data_obj = bucket.get("device_data")
            attrs = getattr(device_data_obj, "attributes", None) or {}
            raw = (
                attrs.get("board")
                or attrs.get("device_type")
                or attrs.get("model")
                or cfg.get(CONF_MODEL)
            )
            if isinstance(raw, str):
                board_for_log = _norm(raw)
            break
    except Exception:
        _LOGGER.debug("NUBLY HA: board resolution for log failed", exc_info=True)

    _LOGGER.info(
        "NUBLY HA: publishing runtime config device=%s board=%s topic=%s "
        "schema_version=%s mode=%s room=%r lights=%d scenes=%d "
        "screen_order=%s screensaver_type=%s screensaver_enabled=%s "
        "screensaver_timeout=%s retain=True",
        device_id,
        board_for_log,
        topic,
        payload.get("schema_version"),
        payload.get("mode"),
        room.get("name"),
        len(room.get("lights") or []),
        len(room.get("scenes") or []),
        screen_order or "<default>",
        payload["screensaver"]["type"],
        payload["screensaver"]["enabled"],
        payload["screensaver"]["timeout_seconds"],
    )
    _LOGGER.debug("NUBLY HA: config payload = %s", payload)

    service_data = {
        "topic": topic,
        "payload": json.dumps(payload),
        "qos": 0,
        "retain": True,
    }

    for attempt in range(1, _PUBLISH_MAX_ATTEMPTS + 1):
        _LOGGER.debug(
            "NUBLY HA: config publish attempt %s/%s",
            attempt,
            _PUBLISH_MAX_ATTEMPTS,
        )
        try:
            await hass.services.async_call(
                "mqtt", "publish", service_data, blocking=True
            )
        except HomeAssistantError as err:
            _LOGGER.warning(
                "NUBLY HA: config publish failed, retrying (%s)", err
            )
        except Exception:
            _LOGGER.exception(
                "NUBLY HA: config publish failed (unexpected error)"
            )
            return
        else:
            _LOGGER.debug("NUBLY HA: config publish ok")
            return

        if attempt < _PUBLISH_MAX_ATTEMPTS:
            await asyncio.sleep(_PUBLISH_RETRY_DELAY_SECONDS)

    _LOGGER.error(
        "NUBLY HA: config publish failed after %s attempts — giving up",
        _PUBLISH_MAX_ATTEMPTS,
    )


def _register_cover_art_view(hass: HomeAssistant) -> None:
    """Register the cover-art view once, idempotently."""
    flag_key = "_cover_art_view_registered"
    if hass.data[DOMAIN].get(flag_key):
        return
    hass.http.register_view(NublyCoverArtView(hass))
    hass.data[DOMAIN][flag_key] = True


async def _async_check_provisioning_once(hass: HomeAssistant) -> None:
    """Run the provisioning detection probe once per HA process."""
    flag_key = "_provisioning_checked"
    if hass.data[DOMAIN].get(flag_key):
        return
    await async_check_provisioning_support(hass)
    hass.data[DOMAIN][flag_key] = True


def _stash_favorites_cache(
    hass: HomeAssistant,
    device_id: str,
    favorites: list[dict],
    children_map: dict[str, list[dict]],
) -> None:
    """Cache the published favorites + pre-traversed category children.

    Stored on the per-entry bucket so the MQTT command handlers can look
    up children synchronously without re-issuing a live browse.
    """
    ids: set[str] = {
        f["media_content_id"]
        for f in favorites
        if isinstance(f, dict) and f.get("media_content_id")
    }
    for children in children_map.values():
        for child in children:
            if isinstance(child, dict) and child.get("media_content_id"):
                ids.add(child["media_content_id"])

    for bucket in (hass.data.get(DOMAIN) or {}).values():
        if not isinstance(bucket, dict):
            continue
        cfg = bucket.get("config") or {}
        if cfg.get(CONF_DEVICE_ID) == device_id:
            bucket["favorite_ids"] = ids
            bucket["favorite_children_map"] = dict(children_map)
            return


async def _resolve_media_favorites(
    hass: HomeAssistant, device_id: str, media_cfg: dict
) -> tuple[list[dict], dict[str, list[dict]]]:
    """Auto-discover favorites for the configured media_player.

    Detection order:
      1. Verify the entity belongs to the Sonos integration (entity registry
         platform == 'sonos'). Non-Sonos entities are skipped.
      2. Try Media Browser via the entity's `async_browse_media` — locate the
         Favorites node and enumerate its children.
      3. Fall back to `sensor.sonos_favorites` (HA's per-system favorites
         sensor) when the browser path returns nothing.

    The resolver never raises — every failure path logs the exact reason
    and returns []. The runtime config simply omits favorites in that case.
    """
    entity_id = (media_cfg.get("entity_id") or "").strip()
    if not entity_id:
        return [], {}

    max_count = int(media_cfg.get("favorites_max") or 12)

    if not _is_sonos_entity(hass, entity_id):
        _LOGGER.debug(
            "NUBLY HA: favorites skipped — entity=%s is not from the Sonos "
            "integration (device=%s)",
            entity_id,
            device_id,
        )
        return [], {}

    _LOGGER.debug(
        "NUBLY HA: favorites Sonos detected for entity=%s device=%s — "
        "attempting Media Browser discovery",
        entity_id,
        device_id,
    )

    favorites, children_map = await _favorites_via_browser(
        hass, entity_id, max_count
    )
    source = "media_browser"
    if not favorites:
        favorites = _favorites_via_sensor(hass, max_count)
        children_map = {}
        source = "sensor.sonos_favorites"

    _LOGGER.info(
        "NUBLY HA: media favorites discovered device=%s entity=%s source=%s "
        "count=%d max=%d categories_with_children=%d",
        device_id,
        entity_id,
        source if favorites else "<none>",
        len(favorites),
        max_count,
        sum(1 for v in children_map.values() if v),
    )
    _LOGGER.debug(
        "NUBLY HA: media favorites entries=%s children_map_keys=%s",
        [{"id": f["id"], "title": f["title"]} for f in favorites],
        list(children_map.keys()),
    )
    return favorites, children_map


def _is_sonos_entity(hass: HomeAssistant, entity_id: str) -> bool:
    """True when the entity is registered against the Sonos integration."""
    try:
        registry = er.async_get(hass)
        entry = registry.async_get(entity_id)
        return bool(entry and entry.platform == "sonos")
    except Exception:
        _LOGGER.debug(
            "NUBLY HA: Sonos detection raised for entity=%s — treating as non-Sonos",
            entity_id,
            exc_info=True,
        )
        return False


async def _favorites_via_browser(
    hass: HomeAssistant, entity_id: str, max_count: int
) -> tuple[list[dict], dict[str, list[dict]]]:
    """Enumerate Sonos favorites and pre-traverse expandable categories.

    Returns (top_level_favorites, children_map). `children_map` is keyed
    by an expandable parent's `media_content_id` and holds the normalized
    runtime entries for each of that parent's children, so the
    `media/browse` command handler can serve them synchronously without
    a second live browse round-trip.
    """
    component = hass.data.get("media_player")
    if component is None:
        _LOGGER.debug("NUBLY HA: favorites browser — media_player component unavailable")
        return [], {}
    player = component.get_entity(entity_id)
    if player is None:
        _LOGGER.debug("NUBLY HA: favorites browser — entity %s not registered", entity_id)
        return [], {}

    try:
        root = await player.async_browse_media()
    except Exception as err:
        _LOGGER.debug(
            "NUBLY HA: favorites browser root raised for %s: %s",
            entity_id,
            err,
        )
        return [], {}

    favorites_node = None
    for child in (getattr(root, "children", None) or []):
        cid = (getattr(child, "media_content_id", "") or "").lower()
        title = (getattr(child, "title", "") or "").lower()
        if "favorit" in title or "favorit" in cid:
            favorites_node = child
            break
    if favorites_node is None:
        _LOGGER.debug(
            "NUBLY HA: favorites browser — no Favorites child in root for %s",
            entity_id,
        )
        return [], {}

    try:
        contents = await player.async_browse_media(
            getattr(favorites_node, "media_content_type", None),
            getattr(favorites_node, "media_content_id", None),
        )
    except Exception as err:
        _LOGGER.debug(
            "NUBLY HA: favorites browser favorites-node raised for %s: %s",
            entity_id,
            err,
        )
        return [], {}

    out: list[dict] = []
    seen_ids: set[str] = set()
    expandable_children = list(getattr(contents, "children", None) or [])
    for raw_child in expandable_children:
        entry = _browse_child_to_runtime(raw_child)
        if entry is None:
            continue
        if entry["media_content_id"] in seen_ids:
            continue
        seen_ids.add(entry["media_content_id"])
        entry["id"] = f"fav_{len(out) + 1}"
        out.append(entry)
        if len(out) >= max_count:
            break

    # Pre-traverse expandable top-level entries so the device can render
    # nested content (Albums/Playlists/Radio/Tracks) without a live
    # browse round-trip from the MQTT command handler.
    children_map: dict[str, list[dict]] = {}
    for fav in out:
        raw = next(
            (
                c
                for c in expandable_children
                if getattr(c, "media_content_id", None) == fav["media_content_id"]
            ),
            None,
        )
        if raw is None or not fav.get("expandable"):
            continue
        await _populate_children_for(player, raw, fav, children_map, entity_id)

    # Fallback: any expandable category that came back empty from live
    # browse — and there are many Sonos configurations where these
    # nested browses return nothing — gets backfilled from the flat
    # sensor.sonos_favorites list, grouped by detected media class.
    empty_categories = [
        fav
        for fav in out
        if fav.get("expandable")
        and not (children_map.get(fav["media_content_id"]) or [])
    ]
    if empty_categories:
        _LOGGER.info(
            "NUBLY HA: %d category/categories returned no children via live "
            "browse for entity=%s — backfilling from sensor.sonos_favorites",
            len(empty_categories),
            entity_id,
        )
        sensor_groups = _build_sensor_category_map(hass)
        for fav in empty_categories:
            title = fav.get("title") or ""
            slug = _category_slug_for_title(title)
            children = sensor_groups.get(slug) or []
            if not children:
                _LOGGER.warning(
                    "NUBLY HA: category %r (slug=%s) still empty after sensor "
                    "backfill (sensor groups: %s)",
                    title,
                    slug,
                    {k: len(v) for k, v in sensor_groups.items()},
                )
                continue
            children_map[fav["media_content_id"]] = children
            if title:
                children_map[title] = children
                children_map[title.lower()] = children
            _LOGGER.info(
                "NUBLY HA: category backfilled title=%r slug=%s children=%d",
                title,
                slug,
                len(children),
            )

    return out, children_map


_SONOS_CATEGORY_SLUGS = ("albums", "playlists", "radio", "tracks", "other")


def _build_sensor_category_map(hass: HomeAssistant) -> dict[str, list[dict]]:
    """Group flat sonos_favorites items by detected category.

    Returns {"albums": [...], "playlists": [...], "radio": [...],
             "tracks": [...], "other": [...]}, each list normalized to
    runtime payload dicts ({title, media_content_id, media_content_type,
    playable, expandable, icon?}).
    """
    state = hass.states.get("sensor.sonos_favorites")
    if state is None:
        _LOGGER.debug(
            "NUBLY HA: sensor.sonos_favorites not present; cannot backfill"
        )
        return {slug: [] for slug in _SONOS_CATEGORY_SLUGS}

    items = state.attributes.get("items") or []
    groups: dict[str, list[dict]] = {slug: [] for slug in _SONOS_CATEGORY_SLUGS}
    if not isinstance(items, list):
        return groups

    counters: dict[str, int] = {slug: 0 for slug in _SONOS_CATEGORY_SLUGS}
    for item in items:
        if not isinstance(item, dict):
            continue
        title = item.get("title") or item.get("name")
        content_id = (
            item.get("item_id")
            or item.get("id")
            or item.get("media_content_id")
        )
        if not title or not content_id:
            continue
        slug = _categorize_sonos_favorite(item)
        counters[slug] += 1
        entry: dict = {
            "id": f"item_{counters[slug]}",
            "title": title,
            "media_content_id": content_id,
            "media_content_type": item.get("media_content_type")
            or "favorite_item_id",
            "playable": True,
            "expandable": False,
        }
        icon = item.get("thumbnail") or item.get("icon")
        if icon:
            entry["icon"] = icon
        groups[slug].append(entry)
    return groups


def _categorize_sonos_favorite(item: dict) -> str:
    """Map a single Sonos favorite dict to one of _SONOS_CATEGORY_SLUGS.

    Looks at every plausible field (type, media_class, content_type,
    item_id prefix) and falls back to 'other'.
    """
    haystack = " ".join(
        str(item.get(k) or "")
        for k in (
            "type",
            "media_class",
            "media_content_type",
            "title",
        )
    ).lower()
    item_id = str(
        item.get("item_id")
        or item.get("id")
        or item.get("media_content_id")
        or ""
    )
    item_id_upper = item_id.upper()

    if "playlist" in haystack or item_id_upper.startswith("SQ:") or "PLAYLIST" in item_id_upper:
        return "playlists"
    if "album" in haystack or item_id_upper.startswith("A:ALBUM") or item_id_upper.startswith("AI:"):
        return "albums"
    if (
        "radio" in haystack
        or "station" in haystack
        or item_id_upper.startswith("R:")
        or "RADIO" in item_id_upper
        or "TUNEIN" in item_id_upper
    ):
        return "radio"
    if (
        "track" in haystack
        or "song" in haystack
        or (item_id_upper.startswith("S:") and not item_id_upper.startswith("SQ:"))
    ):
        return "tracks"
    return "other"


def _category_slug_for_title(title: str) -> str:
    """Map a category label (any language/case) to a canonical slug."""
    t = (title or "").strip().lower()
    if not t:
        return "other"
    if "playlist" in t or "spillel" in t:  # English + Norwegian
        return "playlists"
    if "album" in t:
        return "albums"
    if "radio" in t or "stasjon" in t:
        return "radio"
    if "track" in t or "song" in t or "spor" in t or "sang" in t:
        return "tracks"
    return "other"


async def _populate_children_for(
    player,
    parent_node,
    parent_runtime: dict,
    children_map: dict[str, list[dict]],
    entity_id: str,
) -> None:
    """Browse the children of one expandable parent and store them in the map."""
    parent_id = parent_runtime["media_content_id"]
    try:
        sub = await player.async_browse_media(
            getattr(parent_node, "media_content_type", None),
            parent_id,
        )
    except Exception as err:
        _LOGGER.info(
            "NUBLY HA: favorites pre-traverse failed parent=%s title=%r: %s",
            parent_id,
            parent_runtime.get("title"),
            err,
        )
        children_map[parent_id] = []
        return

    children: list[dict] = []
    for idx, raw_child in enumerate(
        getattr(sub, "children", None) or [], start=1
    ):
        ent = _browse_child_to_runtime(raw_child)
        if ent is None:
            continue
        ent["id"] = f"item_{idx}"
        children.append(ent)

    children_map[parent_id] = children
    # Also index by title to make label-based lookups (e.g. the device
    # sending "Playlists" instead of the real content_id) trivially work.
    title = (parent_runtime.get("title") or "").strip()
    if title and title not in children_map:
        children_map[title] = children
    title_lc = title.lower()
    if title_lc and title_lc != title and title_lc not in children_map:
        children_map[title_lc] = children

    _LOGGER.info(
        "NUBLY HA: favorites pre-traverse parent=%s title=%r children=%d "
        "entity=%s",
        parent_id,
        title,
        len(children),
        entity_id,
    )


def _browse_child_to_runtime(child) -> dict | None:
    """Normalize a BrowseMedia child into the runtime-payload dict shape.

    Captures `playable`/`expandable` so the device can decide whether the
    item plays directly or has to be browsed into for its children.
    """
    title = getattr(child, "title", None)
    content_id = getattr(child, "media_content_id", None)
    if not title or not content_id:
        return None
    entry: dict = {
        "title": title,
        "media_content_id": content_id,
        "media_content_type": getattr(child, "media_content_type", None)
        or "favorite_item_id",
        "playable": bool(getattr(child, "can_play", False)),
        "expandable": bool(getattr(child, "can_expand", False)),
    }
    thumbnail = getattr(child, "thumbnail", None)
    if thumbnail:
        entry["icon"] = thumbnail
    return entry


def _favorites_via_sensor(hass: HomeAssistant, max_count: int) -> list[dict]:
    """Fallback: read `sensor.sonos_favorites` attributes."""
    state = hass.states.get("sensor.sonos_favorites")
    if state is None:
        _LOGGER.debug("NUBLY HA: favorites sensor sensor.sonos_favorites not found")
        return []
    items = state.attributes.get("items") or []
    if not isinstance(items, list):
        return []

    out: list[dict] = []
    seen_ids: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        title = item.get("title") or item.get("name")
        content_id = (
            item.get("item_id") or item.get("id") or item.get("media_content_id")
        )
        if not title or not content_id or content_id in seen_ids:
            continue
        seen_ids.add(content_id)
        entry: dict = {
            "id": f"fav_{len(out) + 1}",
            "title": title,
            "media_content_id": content_id,
            "media_content_type": item.get("media_content_type")
            or "favorite_item_id",
            # Sensor-sourced favorites are always direct playable items.
            "playable": True,
            "expandable": False,
        }
        icon = item.get("thumbnail") or item.get("icon")
        if icon:
            entry["icon"] = icon
        out.append(entry)
        if len(out) >= max_count:
            break
    return out


def _scene_buttons_payload(stored) -> list[dict]:
    """Translate structured scene_buttons into the runtime MQTT shape.

    Preserves order. Disabled entries are kept in the array with
    enabled=false so the device can grey out the slot rather than
    silently shrinking the layout. Entity-less entries are dropped.
    """
    if not isinstance(stored, list):
        return []
    out: list[dict] = []
    for entry in stored:
        if not isinstance(entry, dict):
            continue
        target = (
            entry.get("target_entity") or entry.get("entity_id") or ""
        ).strip()
        if not target:
            continue
        scene: dict = {
            "id": entry.get("id") or f"scene_{len(out) + 1}",
            "label": entry.get("label") or target,
            "icon": (entry.get("icon") or "").strip(),
            "target_entity": target,
            "enabled": bool(entry.get("enabled", True)),
        }
        out.append(scene)
    _LOGGER.debug(
        "NUBLY HA: scene_buttons payload built count=%d entries=%s",
        len(out),
        out,
    )
    return out


def _entity_friendly_name(hass: HomeAssistant, entity_id: str) -> str:
    """Return the entity's friendly_name, or the entity_id stripped of domain."""
    state = hass.states.get(entity_id)
    if state is not None:
        name = state.attributes.get("friendly_name")
        if isinstance(name, str) and name:
            return name
    return entity_id.split(".", 1)[-1].replace("_", " ").title()


def _slugify_room_id(name: str | None) -> str:
    """Lowercase, snake_case slug of a room display name."""
    if not name:
        return ""
    out = []
    prev_us = False
    for ch in name.strip().lower():
        if ch.isalnum():
            out.append(ch)
            prev_us = False
        elif not prev_us:
            out.append("_")
            prev_us = True
    return "".join(out).strip("_")


def _build_cover_art_url(hass: HomeAssistant, device_id: str) -> str:
    """Return an absolute cover art URL if HA has one, else a relative path."""
    path = f"/api/nubly/{device_id}/cover_art"
    try:
        base = get_url(hass, allow_internal=True, prefer_external=False)
    except NoURLAvailableError:
        return path
    return f"{base.rstrip('/')}{path}"


async def _clear_legacy_config(hass: HomeAssistant) -> None:
    """Remove the retained config at the old hardcoded legacy topic."""
    legacy_topic = f"nubly/devices/{LEGACY_DEVICE_ID}/config"
    _LOGGER.debug("NUBLY HA: clearing legacy config topic = %s", legacy_topic)
    await hass.services.async_call(
        "mqtt",
        "publish",
        {
            "topic": legacy_topic,
            "payload": "",
            "qos": 0,
            "retain": True,
        },
    )
