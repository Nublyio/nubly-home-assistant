"""Constants for the Nubly integration."""

DOMAIN = "nubly"

CONF_DEVICE_ID = "device_id"
CONF_ROOM_NAME = "room_name"
CONF_MEDIA_ENTITY = "media_entity"
CONF_LIGHT_ENTITY = "light_entity"
CONF_LIGHT_DISPLAY_NAME = "light_display_name"
CONF_WEATHER_ENTITY = "weather_entity"

# Nubly devices publish a retained JSON attributes message on this topic so HA
# can discover their hardware-generated device_id (format: nubly_<12 hex chars>).
# Topic: nubly/devices/<device_id>/attributes
# Payload: {"device_id": "nubly_...", ...}
DISCOVERY_SUB_TOPIC = "nubly/devices/+/attributes"

# Seconds to listen for device announcements during a discovery round.
DISCOVERY_TIMEOUT = 30.0

# Hardcoded device_id used by the pre-hardware-ID version of this integration.
LEGACY_DEVICE_ID = "nubly_gjesterom_display"
