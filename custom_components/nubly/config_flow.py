"""Config flow for the Nubly integration."""

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .const import (
    CONF_DEVICE_ID,
    CONF_HOST,
    CONF_LIGHT_DISPLAY_NAME,
    CONF_LIGHT_ENTITY,
    CONF_MEDIA_ENTITY,
    CONF_MODEL,
    CONF_PORT,
    CONF_ROOM_NAME,
    CONF_SCREENSAVER_TIMEOUT,
    CONF_SW_VERSION,
    CONF_WEATHER_ENTITY,
    DEFAULT_SCREENSAVER_TIMEOUT,
    DOMAIN,
)
from .discovery import async_discover_devices
from .provisioning import async_provision_device

_LOGGER = logging.getLogger(__name__)


CONFIGURE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ROOM_NAME): str,
        vol.Required(CONF_MEDIA_ENTITY): str,
        vol.Required(CONF_LIGHT_ENTITY): str,
        vol.Required(CONF_LIGHT_DISPLAY_NAME): str,
        vol.Optional(CONF_WEATHER_ENTITY): EntitySelector(
            EntitySelectorConfig(domain="weather"),
        ),
        vol.Required(
            CONF_SCREENSAVER_TIMEOUT,
            default=DEFAULT_SCREENSAVER_TIMEOUT,
        ): NumberSelector(
            NumberSelectorConfig(
                min=5,
                max=3600,
                step=5,
                mode=NumberSelectorMode.BOX,
                unit_of_measurement="s",
            ),
        ),
    }
)


def _ha_mqtt_available(hass: HomeAssistant) -> bool:
    """Return True if HA's MQTT integration has an active config entry."""
    return bool(hass.config_entries.async_entries("mqtt"))


class NublyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Nubly."""

    VERSION = 2

    def __init__(self) -> None:
        self._discovered: list[str] = []
        self._device_id: str | None = None
        self._discovery_fields: dict = {}

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ):
        """Handle a device announced via mDNS/zeroconf."""
        _LOGGER.warning(
            "NUBLY HA: async_step_zeroconf called name=%s type=%s host=%s port=%s",
            getattr(discovery_info, "name", None),
            getattr(discovery_info, "type", None),
            discovery_info.host,
            discovery_info.port,
        )
        if not _ha_mqtt_available(self.hass):
            return self.async_abort(reason="mqtt_not_configured")

        host = discovery_info.host
        port = discovery_info.port
        props = {
            (k.decode() if isinstance(k, bytes) else k): (
                v.decode() if isinstance(v, bytes) else v
            )
            for k, v in (discovery_info.properties or {}).items()
        }

        device_id = props.get("device_id")
        if not device_id:
            return self.async_abort(reason="missing_device_id")

        _LOGGER.warning("NUBLY HA: zeroconf discovered device_id = %s", device_id)
        _LOGGER.warning("NUBLY HA: discovery host = %s:%s", host, port)

        await self.async_set_unique_id(device_id)
        self._abort_if_unique_id_configured(
            updates={CONF_HOST: host, CONF_PORT: port}
        )

        self._device_id = device_id
        self._discovery_fields = {
            CONF_DEVICE_ID: device_id,
            CONF_HOST: host,
            CONF_PORT: port,
            CONF_SW_VERSION: props.get("sw_version"),
            CONF_MODEL: props.get("model"),
        }
        self.context["title_placeholders"] = {"name": device_id}

        return await self.async_step_configure()

    async def async_step_user(self, user_input=None):
        """Manual entry point: fall back to MQTT discovery."""
        if not _ha_mqtt_available(self.hass):
            return self.async_abort(reason="mqtt_not_configured")

        found = await async_discover_devices(self.hass)
        self._discovered = sorted(found)

        if self._discovered:
            return await self.async_step_pick_device()
        return await self.async_step_manual()

    async def async_step_pick_device(self, user_input=None):
        """Let the user pick an MQTT-discovered device."""
        if user_input is not None:
            self._device_id = user_input[CONF_DEVICE_ID]
            self._discovery_fields = {CONF_DEVICE_ID: self._device_id}
            return await self.async_step_configure()

        schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE_ID): SelectSelector(
                    SelectSelectorConfig(
                        options=self._discovered,
                        mode=SelectSelectorMode.DROPDOWN,
                    ),
                ),
            }
        )
        return self.async_show_form(step_id="pick_device", data_schema=schema)

    async def async_step_manual(self, user_input=None):
        """Fallback: manual device_id entry."""
        if user_input is not None:
            self._device_id = user_input[CONF_DEVICE_ID]
            self._discovery_fields = {CONF_DEVICE_ID: self._device_id}
            return await self.async_step_configure()

        schema = vol.Schema({vol.Required(CONF_DEVICE_ID): str})
        return self.async_show_form(step_id="manual", data_schema=schema)

    async def async_step_configure(self, user_input=None):
        """Collect room, entities, weather and screensaver timeout."""
        errors: dict[str, str] = {}

        if user_input is not None:
            if self.unique_id is None and self._device_id:
                await self.async_set_unique_id(self._device_id)
                self._abort_if_unique_id_configured()

            host = self._discovery_fields.get(CONF_HOST)
            if host and self._device_id:
                error_key = await async_provision_device(
                    self.hass, host, self._device_id
                )
                if error_key:
                    errors["base"] = error_key

            if not errors:
                data = {**self._discovery_fields, **user_input}
                title = user_input.get(CONF_ROOM_NAME) or self._device_id
                _LOGGER.warning(
                    "NUBLY HA: config entry created for device_id = %s",
                    self._device_id,
                )
                return self.async_create_entry(title=title, data=data)

        return self.async_show_form(
            step_id="configure",
            data_schema=CONFIGURE_SCHEMA,
            description_placeholders={"device_id": self._device_id or ""},
            errors=errors,
        )
