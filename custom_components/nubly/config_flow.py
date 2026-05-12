"""Config flow for the Nubly integration.

The initial flow is intentionally minimal — it just onboards the device
(discovery, provisioning, room name). Everything else is configured via
the options flow, which presents a menu of sections (General, Room,
Lights, Media, Weather, Screensaver, Scene buttons).
"""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    IconSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .const import (
    CONF_CONFIG,
    CONF_DEVICE_ID,
    CONF_HOST,
    CONF_MODEL,
    CONF_PORT,
    CONF_SW_VERSION,
    CONFIG_SCHEMA_VERSION,
    DEFAULT_SCREENSAVER_TIMEOUT,
    DOMAIN,
)
from .discovery import async_discover_devices
from .firmware import normalize_board, scene_button_count
from .nubly_config import (
    ensure_structured,
    empty_structured,
    replace_section,
    slugify,
)
from .provisioning import async_provision_device

_LOGGER = logging.getLogger(__name__)

# Domains accepted as a scene button target.
_SCENE_TARGET_DOMAINS = ("scene", "script")

_SCREENSAVER_TYPES = ["analog_clock", "digital_clock", "cover_art", "off"]
# Translation key used to localize the screensaver type dropdown.
_SCREENSAVER_TYPE_TRANSLATION_KEY = "screensaver_type"


def _ha_mqtt_available(hass: HomeAssistant) -> bool:
    """Return True if HA's MQTT integration has an active config entry."""
    return bool(hass.config_entries.async_entries("mqtt"))


def _friendly_name_or_id(hass: HomeAssistant, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    if state is not None:
        name = state.attributes.get("friendly_name")
        if isinstance(name, str) and name:
            return name
    return entity_id.split(".", 1)[-1].replace("_", " ").title() if entity_id else ""


# ---------------------------------------------------------------------------
# Initial config flow — onboarding only.
# ---------------------------------------------------------------------------


class NublyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial onboarding flow for a Nubly device."""

    VERSION = 2

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return NublyOptionsFlow(config_entry)

    def __init__(self) -> None:
        self._discovered: list[str] = []
        self._device_id: str | None = None
        self._discovery_fields: dict = {}

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo):
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
        return await self.async_step_onboard()

    async def async_step_user(self, user_input=None):
        if not _ha_mqtt_available(self.hass):
            return self.async_abort(reason="mqtt_not_configured")

        found = await async_discover_devices(self.hass)
        self._discovered = sorted(found)

        if self._discovered:
            return await self.async_step_pick_device()
        return await self.async_step_manual()

    async def async_step_pick_device(self, user_input=None):
        if user_input is not None:
            self._device_id = user_input[CONF_DEVICE_ID]
            self._discovery_fields = {CONF_DEVICE_ID: self._device_id}
            return await self.async_step_onboard()

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
        if user_input is not None:
            self._device_id = user_input[CONF_DEVICE_ID]
            self._discovery_fields = {CONF_DEVICE_ID: self._device_id}
            return await self.async_step_onboard()

        schema = vol.Schema({vol.Required(CONF_DEVICE_ID): str})
        return self.async_show_form(step_id="manual", data_schema=schema)

    async def async_step_onboard(self, user_input=None):
        """Minimal onboarding: confirm device + capture a room name.

        Detailed configuration (lights/media/weather/scenes) is done in
        the options flow after the entry is created.
        """
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
                room_name = (user_input.get("room_name") or "").strip()

                # Seed a structured config with the chosen room name; the
                # options flow fills in the rest.
                structured = empty_structured()
                structured["room"]["name"] = room_name or self._device_id
                structured["room"]["id"] = (
                    slugify(room_name) or (self._device_id or "")
                )

                title = room_name or self._device_id
                _LOGGER.info(
                    "NUBLY HA: config entry created device_id=%s schema_version=%d",
                    self._device_id,
                    CONFIG_SCHEMA_VERSION,
                )
                return self.async_create_entry(
                    title=title,
                    data=self._discovery_fields,
                    options={CONF_CONFIG: structured},
                )

        schema = vol.Schema({vol.Required("room_name", default=""): str})
        return self.async_show_form(
            step_id="onboard",
            data_schema=schema,
            description_placeholders={"device_id": self._device_id or ""},
            errors=errors,
        )


# ---------------------------------------------------------------------------
# Options flow — structured, menu-driven, one section per step.
# ---------------------------------------------------------------------------


class NublyOptionsFlow(OptionsFlow):
    """Edit settings of an already-configured Nubly device.

    Republishes the retained MQTT config payload only. Does not re-run
    Wi-Fi/MQTT provisioning or restart Mosquitto.
    """

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry

    # Snapshot the current structured config at each entry into a step so
    # sub-steps see the latest persisted state.
    def _current(self) -> dict:
        structured, _ = ensure_structured(
            dict(self._config_entry.data), dict(self._config_entry.options)
        )
        return structured

    def _save(self, structured: dict):
        merged_options = {**self._config_entry.options, CONF_CONFIG: structured}
        _LOGGER.info(
            "NUBLY HA: options saved schema_version=%d device_id=%s",
            structured.get("schema_version", CONFIG_SCHEMA_VERSION),
            self._config_entry.data.get(CONF_DEVICE_ID),
        )
        return self.async_create_entry(title="", data=merged_options)

    def _board(self) -> str | None:
        return normalize_board(self._config_entry.data.get(CONF_MODEL))

    async def async_step_init(self, user_input=None):
        _LOGGER.debug("NUBLY HA: options flow init menu entered")
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "general",
                "lights",
                "media",
                "weather",
                "screensaver",
                "scenes",
            ],
        )

    # ---- General (room name) ----

    async def async_step_general(self, user_input=None):
        _LOGGER.debug("NUBLY HA: options flow step=general entered")
        current = self._current()
        if user_input is not None:
            room_name = (user_input.get("room_name") or "").strip()
            updated = replace_section(
                current,
                "room",
                {
                    "id": slugify(room_name) or current["room"].get("id", ""),
                    "name": room_name,
                },
            )
            return self._save(updated)

        return self.async_show_form(
            step_id="general",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "room_name", default=current["room"].get("name", "")
                    ): str,
                }
            ),
        )

    # ---- Lights ----

    async def async_step_lights(self, user_input=None):
        _LOGGER.debug("NUBLY HA: options flow step=lights entered")
        current = self._current()
        existing = (
            (current.get("screens") or {}).get("lights") or {}
        ).get("entities") or []

        if user_input is not None:
            selected = user_input.get("light_entities") or []
            # Drop dropped lights, keep label/icon for kept ones; we'll
            # collect names + icons in a follow-up step.
            existing_by_id = {
                e["entity_id"]: e for e in existing if isinstance(e, dict)
            }
            self._lights_draft = [
                {
                    "entity_id": eid,
                    "label": (existing_by_id.get(eid) or {}).get("label", ""),
                    "icon": (existing_by_id.get(eid) or {}).get("icon", ""),
                }
                for eid in selected
            ]
            if self._lights_draft:
                return await self.async_step_lights_detail()
            updated = replace_section(
                current, "screens.lights", {"entities": []}
            )
            return self._save(updated)

        default_selection = [
            e["entity_id"] for e in existing if isinstance(e, dict)
        ]
        return self.async_show_form(
            step_id="lights",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "light_entities", default=default_selection
                    ): EntitySelector(
                        EntitySelectorConfig(domain="light", multiple=True),
                    ),
                }
            ),
        )

    async def async_step_lights_detail(self, user_input=None):
        _LOGGER.debug("NUBLY HA: options flow step=lights_detail entered")
        current = self._current()
        drafts: list[dict] = getattr(self, "_lights_draft", [])

        if user_input is not None:
            entities = []
            for i, draft in enumerate(drafts, start=1):
                eid = draft["entity_id"]
                label = (user_input.get(f"light_{i}_label") or "").strip()
                icon = (user_input.get(f"light_{i}_icon") or "").strip()
                entities.append(
                    {
                        "entity_id": eid,
                        "label": label or _friendly_name_or_id(self.hass, eid),
                        "icon": icon,
                    }
                )
            updated = replace_section(
                current, "screens.lights", {"entities": entities}
            )
            return self._save(updated)

        schema_dict: dict = {}
        for i, draft in enumerate(drafts, start=1):
            eid = draft["entity_id"]
            label_default = draft["label"] or _friendly_name_or_id(self.hass, eid)
            schema_dict[
                vol.Optional(f"light_{i}_label", default=label_default)
            ] = str
            schema_dict[
                vol.Optional(f"light_{i}_icon", default=draft.get("icon", ""))
            ] = IconSelector()
        return self.async_show_form(
            step_id="lights_detail",
            data_schema=vol.Schema(schema_dict),
            description_placeholders={
                "entities": ", ".join(d["entity_id"] for d in drafts)
            },
        )

    # ---- Media ----

    async def async_step_media(self, user_input=None):
        _LOGGER.debug("NUBLY HA: options flow step=media entered")
        current = self._current()
        media_cur = (current.get("screens") or {}).get("media") or {}

        if user_input is not None:
            entity_id = (user_input.get("entity_id") or "").strip()
            label = (user_input.get("label") or "").strip()
            value = (
                {"entity_id": entity_id, "label": label or entity_id}
                if entity_id
                else {}
            )
            updated = replace_section(current, "screens.media", value)
            return self._save(updated)

        schema_dict: dict = {}
        default_entity = media_cur.get("entity_id")
        entity_key = (
            vol.Optional("entity_id", default=default_entity)
            if default_entity
            else vol.Optional("entity_id")
        )
        schema_dict[entity_key] = EntitySelector(
            EntitySelectorConfig(domain="media_player"),
        )
        schema_dict[
            vol.Optional("label", default=media_cur.get("label", ""))
        ] = str
        return self.async_show_form(
            step_id="media", data_schema=vol.Schema(schema_dict)
        )

    # ---- Weather (+ ambient sensors) ----

    async def async_step_weather(self, user_input=None):
        _LOGGER.debug("NUBLY HA: options flow step=weather entered")
        current = self._current()
        weather_cur = (current.get("screens") or {}).get("weather") or {}
        ambient_cur = (current.get("screens") or {}).get("ambient") or {}

        if user_input is not None:
            weather_value = (
                {"entity_id": user_input["weather_entity"]}
                if user_input.get("weather_entity")
                else {}
            )
            ambient_value: dict = {}
            if user_input.get("temperature_entity"):
                ambient_value["temperature_entity"] = user_input["temperature_entity"]
            if user_input.get("humidity_entity"):
                ambient_value["humidity_entity"] = user_input["humidity_entity"]

            updated = replace_section(current, "screens.weather", weather_value)
            updated = replace_section(updated, "screens.ambient", ambient_value)
            return self._save(updated)

        schema_dict: dict = {}
        for key, domain, dc, default in (
            ("weather_entity", "weather", None, weather_cur.get("entity_id")),
            ("temperature_entity", "sensor", "temperature", ambient_cur.get("temperature_entity")),
            ("humidity_entity", "sensor", "humidity", ambient_cur.get("humidity_entity")),
        ):
            field = (
                vol.Optional(key, default=default)
                if default
                else vol.Optional(key)
            )
            cfg = (
                EntitySelectorConfig(domain=domain, device_class=dc)
                if dc
                else EntitySelectorConfig(domain=domain)
            )
            schema_dict[field] = EntitySelector(cfg)

        return self.async_show_form(
            step_id="weather", data_schema=vol.Schema(schema_dict)
        )

    # ---- Screensaver ----

    async def async_step_screensaver(self, user_input=None):
        _LOGGER.debug("NUBLY HA: options flow step=screensaver entered")
        current = self._current()
        ss_cur = current.get("screensaver") or {}

        if user_input is not None:
            new_section = {
                "enabled": bool(user_input.get("enabled", True)),
                "type": user_input.get("type") or "analog_clock",
                "timeout_seconds": int(
                    user_input.get("timeout_seconds")
                    or DEFAULT_SCREENSAVER_TIMEOUT
                ),
            }
            _LOGGER.info(
                "NUBLY HA: screensaver config saved device=%s %s",
                self._config_entry.data.get(CONF_DEVICE_ID),
                new_section,
            )
            updated = replace_section(current, "screensaver", new_section)
            return self._save(updated)

        schema = vol.Schema(
            {
                vol.Required(
                    "enabled", default=bool(ss_cur.get("enabled", True))
                ): BooleanSelector(),
                vol.Required(
                    "type", default=ss_cur.get("type") or "analog_clock"
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=_SCREENSAVER_TYPES,
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key=_SCREENSAVER_TYPE_TRANSLATION_KEY,
                    ),
                ),
                vol.Required(
                    "timeout_seconds",
                    default=int(
                        ss_cur.get("timeout_seconds")
                        or DEFAULT_SCREENSAVER_TIMEOUT
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
        )
        return self.async_show_form(step_id="screensaver", data_schema=schema)

    # ---- Scene buttons ----

    async def async_step_scenes(self, user_input=None):
        _LOGGER.debug("NUBLY HA: options flow step=scenes entered")
        current = self._current()
        existing = current.get("scene_buttons") or []
        board = self._board()
        count = scene_button_count(board)

        if user_input is not None:
            scenes, errors = _scenes_from_form(self.hass, user_input, count)
            if errors:
                return self.async_show_form(
                    step_id="scenes",
                    data_schema=_scenes_schema(self.hass, count, scenes),
                    errors=errors,
                    description_placeholders={"count": str(count)},
                )
            _LOGGER.info(
                "NUBLY HA: scene button config saved device=%s count=%d %s",
                self._config_entry.data.get(CONF_DEVICE_ID),
                len(scenes),
                scenes,
            )
            updated = replace_section(current, "scene_buttons", scenes)
            return self._save(updated)

        return self.async_show_form(
            step_id="scenes",
            data_schema=_scenes_schema(self.hass, count, existing),
            description_placeholders={"count": str(count)},
        )


# ---------------------------------------------------------------------------
# Scene-button helpers
# ---------------------------------------------------------------------------


def _scenes_schema(hass: HomeAssistant, count: int, existing: list) -> vol.Schema:
    existing_by_index: dict[int, dict] = {}
    if isinstance(existing, list):
        for i, item in enumerate(existing):
            if isinstance(item, dict):
                existing_by_index[i] = item

    schema_dict: dict = {}
    for i in range(count):
        prev = existing_by_index.get(i, {})
        n = i + 1
        schema_dict[vol.Optional(f"scene_{n}_label", default=prev.get("label", ""))] = str

        entity_default = prev.get("target_entity") or prev.get("entity_id")
        entity_field = (
            vol.Optional(f"scene_{n}_entity", default=entity_default)
            if entity_default
            else vol.Optional(f"scene_{n}_entity")
        )
        schema_dict[entity_field] = EntitySelector(
            EntitySelectorConfig(domain=list(_SCENE_TARGET_DOMAINS)),
        )
        schema_dict[vol.Optional(f"scene_{n}_icon", default=prev.get("icon", ""))] = IconSelector()
        schema_dict[
            vol.Optional(f"scene_{n}_enabled", default=bool(prev.get("enabled", True)))
        ] = BooleanSelector()
    return vol.Schema(schema_dict)


def _scenes_from_form(
    hass: HomeAssistant, user_input: dict, count: int
) -> tuple[list[dict], dict[str, str]]:
    scenes: list[dict] = []
    errors: dict[str, str] = {}
    for i in range(count):
        n = i + 1
        entity_id = (user_input.get(f"scene_{n}_entity") or "").strip()
        label = (user_input.get(f"scene_{n}_label") or "").strip()
        icon = (user_input.get(f"scene_{n}_icon") or "").strip()
        enabled = bool(user_input.get(f"scene_{n}_enabled", True))

        if not entity_id and not label:
            continue
        if entity_id and hass.states.get(entity_id) is None:
            errors[f"scene_{n}_entity"] = "entity_not_found"
            continue
        scenes.append(
            {
                "id": f"scene_{n}",
                "label": label
                or (
                    hass.states.get(entity_id).attributes.get("friendly_name")
                    if entity_id and hass.states.get(entity_id)
                    else entity_id
                ),
                "target_entity": entity_id,
                "icon": icon,
                "enabled": enabled,
            }
        )
    return scenes, errors
