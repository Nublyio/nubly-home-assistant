"""The Nubly integration."""

import asyncio
import json
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
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
    return True


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
    _LOGGER.info("NUBLY HA: integration setup started for device_id = %s", device_id)

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
        "NUBLY HA: integration setup completed for device_id = %s", device_id
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

    room: dict = {
        "id": room_id,
        "name": room_name,
        "lights": lights,
        "scenes": _scene_buttons_payload(structured.get("scene_buttons")),
    }

    media = screens.get("media") or {}
    if media.get("entity_id"):
        room["media"] = {
            "entity_id": media["entity_id"],
            "cover_art_url": _build_cover_art_url(hass, device_id),
        }
        if media.get("label"):
            room["media"]["label"] = media["label"]

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

    timeout = int(
        screensaver.get("timeout_seconds")
        or DEFAULT_SCREENSAVER_TIMEOUT
    )

    payload = {
        "schema_version": int(structured.get("schema_version") or 2),
        "mode": "room_controller",
        "device_id": device_id,
        "room": room,
        "screensaver": {
            "enabled": bool(screensaver.get("enabled", True)),
            "type": screensaver.get("type") or "analog_clock",
            "timeout_seconds": timeout,
        },
        # Legacy top-level alias still consumed by current firmware until
        # the device branches on schema_version.
        "screensaver_timeout": timeout,
    }

    topic = f"nubly/devices/{device_id}/config"
    _LOGGER.info(
        "NUBLY HA: publishing runtime config device=%s topic=%s "
        "schema_version=%s screensaver_type=%s screensaver_enabled=%s "
        "screensaver_timeout=%s scene_buttons=%d",
        device_id,
        topic,
        payload.get("schema_version"),
        payload["screensaver"]["type"],
        payload["screensaver"]["enabled"],
        payload["screensaver"]["timeout_seconds"],
        len(room.get("scenes") or []),
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


def _scene_buttons_payload(stored) -> list[dict]:
    """Translate structured scene_buttons into the runtime MQTT shape.

    Drops disabled or entity-less entries. Preserves order.
    """
    if not isinstance(stored, list):
        return []
    out: list[dict] = []
    for entry in stored:
        if not isinstance(entry, dict):
            continue
        if not entry.get("enabled", True):
            continue
        target = (
            entry.get("target_entity") or entry.get("entity_id") or ""
        ).strip()
        if not target:
            continue
        scene: dict = {
            "id": entry.get("id") or f"scene_{len(out) + 1}",
            "entity_id": target,
            "label": entry.get("label") or target,
        }
        icon = (entry.get("icon") or "").strip()
        if icon:
            scene["icon"] = icon
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
