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
    CONF_ADDITIONAL_LIGHT_ENTITIES,
    CONF_DEVICE_ID,
    CONF_LIGHT_DISPLAY_NAME,
    CONF_LIGHT_ENTITY,
    CONF_MEDIA_ENTITY,
    CONF_MODEL,
    CONF_ROOM_NAME,
    CONF_SCREENSAVER_TIMEOUT,
    CONF_SW_VERSION,
    CONF_WEATHER_ENTITY,
    DEFAULT_SCREENSAVER_TIMEOUT,
    DOMAIN,
    LEGACY_DEVICE_ID,
)
from .commands import async_subscribe_commands
from .device_data import NublyDeviceData
from .discovery import async_discover_devices
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

    if entry.options:
        data.update(entry.options)

    device_data = NublyDeviceData(hass, data[CONF_DEVICE_ID])
    try:
        await device_data.async_start()
    except Exception:
        _LOGGER.exception(
            "NUBLY HA: failed to subscribe to attributes/availability for %s",
            data[CONF_DEVICE_ID],
        )

    hass.data[DOMAIN][entry.entry_id] = {
        "config": data,
        "device_data": device_data,
    }

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    device_id = data[CONF_DEVICE_ID]
    dev_reg = dr.async_get(hass)
    dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, device_id)},
        manufacturer="Nubly",
        name=data.get(CONF_ROOM_NAME) or device_id,
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
        await _publish_config(hass, data)
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
    data = {**entry.data, **entry.options}
    bucket = hass.data[DOMAIN].get(entry.entry_id)
    if isinstance(bucket, dict):
        bucket["config"] = data

    device_id = data.get(CONF_DEVICE_ID)
    _LOGGER.info(
        "NUBLY HA: options updated for device_id = %s, republishing config",
        device_id,
    )

    if device_id:
        dev_reg = dr.async_get(hass)
        device = dev_reg.async_get_device(identifiers={(DOMAIN, device_id)})
        if device is not None:
            new_name = data.get(CONF_ROOM_NAME) or device_id
            if device.name != new_name:
                dev_reg.async_update_device(device.id, name=new_name)

    try:
        await _publish_config(hass, data)
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


async def _publish_config(hass: HomeAssistant, data: dict) -> None:
    """Publish device configuration to MQTT."""
    device_id = data[CONF_DEVICE_ID]

    room_name = data[CONF_ROOM_NAME]
    room_id = _slugify_room_id(room_name) or device_id

    primary_light_entity = data.get(CONF_LIGHT_ENTITY)
    primary_light_name = (
        data.get(CONF_LIGHT_DISPLAY_NAME) or room_name or device_id
    )

    lights: list[dict] = []
    seen: set[str] = set()
    if primary_light_entity:
        lights.append(
            {
                "entity_id": primary_light_entity,
                "name": primary_light_name,
                "type": "dimmer",
            }
        )
        seen.add(primary_light_entity)

    for extra in data.get(CONF_ADDITIONAL_LIGHT_ENTITIES) or []:
        if not extra or extra in seen:
            continue
        seen.add(extra)
        lights.append(
            {
                "entity_id": extra,
                "name": _entity_friendly_name(hass, extra),
                "type": "dimmer",
            }
        )

    timeout = int(
        data.get(CONF_SCREENSAVER_TIMEOUT, DEFAULT_SCREENSAVER_TIMEOUT)
    )

    room: dict = {
        "id": room_id,
        "name": room_name,
        "lights": lights,
        "scenes": [],
    }

    media_entity = data.get(CONF_MEDIA_ENTITY)
    if media_entity:
        room["media"] = {
            "entity_id": media_entity,
            "cover_art_url": _build_cover_art_url(hass, device_id),
        }

    weather_entity = data.get(CONF_WEATHER_ENTITY)
    if weather_entity:
        room["weather"] = {"entity_id": weather_entity}

    payload = {
        "mode": "room_controller",
        "device_id": device_id,
        "room": room,
        "screensaver_timeout": timeout,
    }

    topic = f"nubly/devices/{device_id}/config"
    _LOGGER.debug("NUBLY HA: publishing config to topic = %s", topic)
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
