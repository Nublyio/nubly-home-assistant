"""The Nubly integration."""

import json

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_DEVICE_ID,
    CONF_LIGHT_DISPLAY_NAME,
    CONF_LIGHT_ENTITY,
    CONF_MEDIA_ENTITY,
    CONF_ROOM_NAME,
    DOMAIN,
)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the Nubly integration."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Nubly from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = entry.data

    await _publish_config(hass, entry.data)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Nubly config entry."""
    hass.data[DOMAIN].pop(entry.entry_id)
    return True


async def _publish_config(hass: HomeAssistant, data: dict) -> None:
    """Publish device configuration to MQTT."""
    device_id = data[CONF_DEVICE_ID]

    payload = {
        "device_id": device_id,
        "room_name": data[CONF_ROOM_NAME],
        "media": {
            "entity_id": data[CONF_MEDIA_ENTITY],
        },
        "light": {
            "entity_id": data[CONF_LIGHT_ENTITY],
            "display_name": data[CONF_LIGHT_DISPLAY_NAME],
        },
        "screens": {
            "media_enabled": True,
            "light_enabled": True,
            "clock_enabled": True,
        },
        "screensaver_timeout_seconds": 30,
    }

    await hass.services.async_call(
        "mqtt",
        "publish",
        {
            "topic": f"nubly/devices/{device_id}/config",
            "payload": json.dumps(payload),
            "qos": 0,
            "retain": True,
        },
    )
