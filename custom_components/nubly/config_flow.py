"""Config flow for the Nubly integration."""

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import HomeAssistant, callback
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
    CONF_ADDITIONAL_LIGHT_ENTITIES,
    CONF_DEVICE_ID,
    CONF_HOST,
    CONF_HUMIDITY_ENTITY,
    CONF_LIGHT_DISPLAY_NAME,
    CONF_LIGHT_ENTITY,
    CONF_LIGHT_NAMES,
    CONF_MEDIA_ENTITY,
    CONF_MODEL,
    CONF_PORT,
    CONF_ROOM_NAME,
    CONF_SCREENSAVER_TIMEOUT,
    CONF_SW_VERSION,
    CONF_TEMPERATURE_ENTITY,
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
        vol.Required(CONF_MEDIA_ENTITY): EntitySelector(
            EntitySelectorConfig(domain="media_player"),
        ),
        vol.Required(CONF_LIGHT_ENTITY): EntitySelector(
            EntitySelectorConfig(domain="light"),
        ),
        vol.Required(CONF_LIGHT_DISPLAY_NAME): str,
        vol.Optional(CONF_ADDITIONAL_LIGHT_ENTITIES, default=[]): EntitySelector(
            EntitySelectorConfig(domain="light", multiple=True),
        ),
        vol.Optional(CONF_WEATHER_ENTITY): EntitySelector(
            EntitySelectorConfig(domain="weather"),
        ),
        vol.Optional(CONF_TEMPERATURE_ENTITY): EntitySelector(
            EntitySelectorConfig(
                domain="sensor", device_class="temperature"
            ),
        ),
        vol.Optional(CONF_HUMIDITY_ENTITY): EntitySelector(
            EntitySelectorConfig(domain="sensor", device_class="humidity"),
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


def _entity_friendly_name_or_id(hass: HomeAssistant, entity_id: str) -> str:
    """Best-effort default name for a light: friendly_name then entity_id tail."""
    state = hass.states.get(entity_id)
    if state is not None:
        name = state.attributes.get("friendly_name")
        if isinstance(name, str) and name:
            return name
    return entity_id.split(".", 1)[-1].replace("_", " ").title()


def _ha_mqtt_available(hass: HomeAssistant) -> bool:
    """Return True if HA's MQTT integration has an active config entry."""
    return bool(hass.config_entries.async_entries("mqtt"))


class NublyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Nubly."""

    VERSION = 2

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow for editing an existing entry."""
        return NublyOptionsFlow(config_entry)

    def __init__(self) -> None:
        self._discovered: list[str] = []
        self._device_id: str | None = None
        self._discovery_fields: dict = {}
        self._configure_input: dict | None = None

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ):
        """Handle a device announced via mDNS/zeroconf."""
        _LOGGER.debug(
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

        _LOGGER.info("NUBLY HA: zeroconf discovered device_id = %s", device_id)
        _LOGGER.debug("NUBLY HA: discovery host = %s:%s", host, port)

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
                self._configure_input = user_input
                if user_input.get(CONF_ADDITIONAL_LIGHT_ENTITIES):
                    return await self.async_step_light_names()
                return self._finalize_entry({})

        return self.async_show_form(
            step_id="configure",
            data_schema=CONFIGURE_SCHEMA,
            description_placeholders={"device_id": self._device_id or ""},
            errors=errors,
        )

    async def async_step_light_names(self, user_input=None):
        """Per-light optional friendly names for additional lights."""
        assert self._configure_input is not None
        extras: list[str] = list(
            self._configure_input.get(CONF_ADDITIONAL_LIGHT_ENTITIES) or []
        )

        if user_input is not None:
            names = {
                eid: (user_input.get(eid) or "").strip()
                for eid in extras
                if (user_input.get(eid) or "").strip()
            }
            return self._finalize_entry({CONF_LIGHT_NAMES: names})

        schema_dict: dict = {}
        for eid in extras:
            default = _entity_friendly_name_or_id(self.hass, eid)
            schema_dict[vol.Optional(eid, default=default)] = str

        return self.async_show_form(
            step_id="light_names",
            data_schema=vol.Schema(schema_dict),
            description_placeholders={"device_id": self._device_id or ""},
        )

    def _finalize_entry(self, extra_data: dict):
        assert self._configure_input is not None
        data = {
            **self._discovery_fields,
            **self._configure_input,
            **extra_data,
        }
        title = self._configure_input.get(CONF_ROOM_NAME) or self._device_id
        _LOGGER.info(
            "NUBLY HA: config entry created for device_id = %s",
            self._device_id,
        )
        return self.async_create_entry(title=title, data=data)


class NublyOptionsFlow(OptionsFlow):
    """Edit settings of an already-configured Nubly device.

    Republishes the retained MQTT config payload only. Does not re-run
    Wi-Fi/MQTT provisioning or restart Mosquitto.
    """

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry
        self._init_input: dict | None = None

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            cleaned = {k: v for k, v in user_input.items() if v is not None}
            extras = cleaned.get(CONF_ADDITIONAL_LIGHT_ENTITIES) or []
            if extras:
                self._init_input = cleaned
                return await self.async_step_light_names()
            cleaned[CONF_LIGHT_NAMES] = {}
            return self.async_create_entry(title="", data=cleaned)

        current = {**self._config_entry.data, **self._config_entry.options}

        schema_dict: dict = {
            vol.Required(
                CONF_ROOM_NAME,
                default=current.get(CONF_ROOM_NAME, ""),
            ): str,
            vol.Required(
                CONF_MEDIA_ENTITY,
                default=current.get(CONF_MEDIA_ENTITY),
            ): EntitySelector(
                EntitySelectorConfig(domain="media_player"),
            ),
            vol.Required(
                CONF_LIGHT_ENTITY,
                default=current.get(CONF_LIGHT_ENTITY),
            ): EntitySelector(
                EntitySelectorConfig(domain="light"),
            ),
            vol.Required(
                CONF_LIGHT_DISPLAY_NAME,
                default=current.get(CONF_LIGHT_DISPLAY_NAME, ""),
            ): str,
            vol.Optional(
                CONF_ADDITIONAL_LIGHT_ENTITIES,
                default=current.get(CONF_ADDITIONAL_LIGHT_ENTITIES, []) or [],
            ): EntitySelector(
                EntitySelectorConfig(domain="light", multiple=True),
            ),
            vol.Required(
                CONF_SCREENSAVER_TIMEOUT,
                default=current.get(
                    CONF_SCREENSAVER_TIMEOUT, DEFAULT_SCREENSAVER_TIMEOUT
                ),
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

        weather_default = current.get(CONF_WEATHER_ENTITY)
        weather_key = (
            vol.Optional(CONF_WEATHER_ENTITY, default=weather_default)
            if weather_default
            else vol.Optional(CONF_WEATHER_ENTITY)
        )
        schema_dict[weather_key] = EntitySelector(
            EntitySelectorConfig(domain="weather"),
        )

        for conf_key, device_class in (
            (CONF_TEMPERATURE_ENTITY, "temperature"),
            (CONF_HUMIDITY_ENTITY, "humidity"),
        ):
            existing = current.get(conf_key)
            key = (
                vol.Optional(conf_key, default=existing)
                if existing
                else vol.Optional(conf_key)
            )
            schema_dict[key] = EntitySelector(
                EntitySelectorConfig(
                    domain="sensor", device_class=device_class
                ),
            )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema_dict),
        )

    async def async_step_light_names(self, user_input=None):
        """Per-light optional names for additional lights (options flow)."""
        assert self._init_input is not None
        extras: list[str] = list(
            self._init_input.get(CONF_ADDITIONAL_LIGHT_ENTITIES) or []
        )
        existing_names: dict = (
            self._config_entry.options.get(CONF_LIGHT_NAMES)
            or self._config_entry.data.get(CONF_LIGHT_NAMES)
            or {}
        )

        if user_input is not None:
            names = {
                eid: (user_input.get(eid) or "").strip()
                for eid in extras
                if (user_input.get(eid) or "").strip()
            }
            data = {**self._init_input, CONF_LIGHT_NAMES: names}
            return self.async_create_entry(title="", data=data)

        schema_dict: dict = {}
        for eid in extras:
            default = existing_names.get(eid) or _entity_friendly_name_or_id(
                self.hass, eid
            )
            schema_dict[vol.Optional(eid, default=default)] = str

        return self.async_show_form(
            step_id="light_names",
            data_schema=vol.Schema(schema_dict),
        )
