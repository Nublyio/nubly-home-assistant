"""Structured per-device configuration model with backward-compat migration.

Storage shape (lives at entry.options[CONF_CONFIG]):

    {
      "schema_version": 2,
      "room":  {"id": "...", "name": "..."},
      "screens": {
        "lights":  {"entities": [{"entity_id": "...", "label": "...", "icon": "..."}]},
        "media":   {"entity_id": "...", "label": "..."},
        "weather": {"entity_id": "..."},
        "ambient": {"temperature_entity": "...", "humidity_entity": "..."}
      },
      "screensaver": {
        "enabled": true,
        "type": "analog_clock",
        "timeout_seconds": 60
      },
      "scene_buttons": [
        {"id": "scene_1", "label": "Kos", "target_entity": "...", "icon": "mdi:sofa", "enabled": true}
      ]
    }
"""

from __future__ import annotations

import logging
from typing import Any

from .const import (
    CONF_ADDITIONAL_LIGHT_ENTITIES,
    CONF_CONFIG,
    CONF_HUMIDITY_ENTITY,
    CONF_LIGHT_DISPLAY_NAME,
    CONF_LIGHT_ENTITY,
    CONF_LIGHT_NAMES,
    CONF_MEDIA_ENTITY,
    CONF_ROOM_NAME,
    CONF_SCENES,
    CONF_SCREENSAVER_TIMEOUT,
    CONF_TEMPERATURE_ENTITY,
    CONF_WEATHER_ENTITY,
    CONFIG_SCHEMA_VERSION,
    DEFAULT_SCREENSAVER_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)


def slugify(value: str | None) -> str:
    if not value:
        return ""
    out: list[str] = []
    prev_underscore = False
    for ch in value.strip().lower():
        if ch.isalnum():
            out.append(ch)
            prev_underscore = False
        elif not prev_underscore:
            out.append("_")
            prev_underscore = True
    return "".join(out).strip("_")


def empty_structured() -> dict:
    return {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "room": {"id": "", "name": ""},
        "screens": {
            "lights": {"entities": []},
            "media": {},
            "weather": {},
            "ambient": {},
        },
        "screensaver": {
            "enabled": True,
            "type": "analog_clock",
            "timeout_seconds": DEFAULT_SCREENSAVER_TIMEOUT,
        },
        "scene_buttons": [],
    }


def migrate_flat(flat: dict[str, Any]) -> dict:
    """Build a structured config dict from legacy flat keys.

    Inputs are read from a merged data+options dict captured by the
    legacy config flow. Missing fields are filled with safe defaults.
    """
    structured = empty_structured()

    room_name = (flat.get(CONF_ROOM_NAME) or "").strip()
    structured["room"]["name"] = room_name
    structured["room"]["id"] = slugify(room_name)

    # Lights: primary + extras with their names.
    light_entities: list[dict] = []
    primary = flat.get(CONF_LIGHT_ENTITY)
    if isinstance(primary, str) and primary:
        light_entities.append(
            {
                "entity_id": primary,
                "label": (flat.get(CONF_LIGHT_DISPLAY_NAME) or "").strip()
                or room_name
                or primary,
                "icon": "",
            }
        )
    light_names = flat.get(CONF_LIGHT_NAMES) or {}
    if not isinstance(light_names, dict):
        light_names = {}
    seen = {primary} if primary else set()
    for extra in flat.get(CONF_ADDITIONAL_LIGHT_ENTITIES) or []:
        if not extra or extra in seen:
            continue
        seen.add(extra)
        light_entities.append(
            {
                "entity_id": extra,
                "label": (light_names.get(extra) or "").strip() or extra,
                "icon": "",
            }
        )
    structured["screens"]["lights"]["entities"] = light_entities

    media_entity = flat.get(CONF_MEDIA_ENTITY)
    if isinstance(media_entity, str) and media_entity:
        structured["screens"]["media"] = {
            "entity_id": media_entity,
            "label": room_name or media_entity,
        }

    weather_entity = flat.get(CONF_WEATHER_ENTITY)
    if isinstance(weather_entity, str) and weather_entity:
        structured["screens"]["weather"] = {"entity_id": weather_entity}

    ambient: dict = {}
    temperature_entity = flat.get(CONF_TEMPERATURE_ENTITY)
    if isinstance(temperature_entity, str) and temperature_entity:
        ambient["temperature_entity"] = temperature_entity
    humidity_entity = flat.get(CONF_HUMIDITY_ENTITY)
    if isinstance(humidity_entity, str) and humidity_entity:
        ambient["humidity_entity"] = humidity_entity
    if ambient:
        structured["screens"]["ambient"] = ambient

    try:
        timeout = int(flat.get(CONF_SCREENSAVER_TIMEOUT, DEFAULT_SCREENSAVER_TIMEOUT))
    except (TypeError, ValueError):
        timeout = DEFAULT_SCREENSAVER_TIMEOUT
    structured["screensaver"]["timeout_seconds"] = timeout

    # Scene buttons.
    stored_scenes = flat.get(CONF_SCENES) or []
    if isinstance(stored_scenes, list):
        out_buttons: list[dict] = []
        for idx, item in enumerate(stored_scenes, start=1):
            if not isinstance(item, dict):
                continue
            target = (item.get("entity_id") or item.get("target_entity") or "").strip()
            if not target:
                continue
            out_buttons.append(
                {
                    "id": item.get("id") or f"scene_{idx}",
                    "label": (item.get("label") or "").strip() or target,
                    "target_entity": target,
                    "icon": (item.get("icon") or "").strip(),
                    "enabled": bool(item.get("enabled", True)),
                }
            )
        structured["scene_buttons"] = out_buttons

    return structured


def ensure_structured(flat_data: dict, options: dict) -> tuple[dict, bool]:
    """Return (structured_config, migrated).

    If options already holds a v2+ config, return it untouched.
    Otherwise build one from the merged flat data + options.
    """
    existing = options.get(CONF_CONFIG)
    if (
        isinstance(existing, dict)
        and int(existing.get("schema_version", 0)) >= CONFIG_SCHEMA_VERSION
    ):
        return existing, False
    merged = {**flat_data, **{k: v for k, v in options.items() if k != CONF_CONFIG}}
    structured = migrate_flat(merged)
    _LOGGER.info(
        "NUBLY HA: structured config migration applied schema_version=%d "
        "room=%s lights=%d scenes=%d",
        structured["schema_version"],
        structured["room"].get("name"),
        len(structured["screens"]["lights"]["entities"]),
        len(structured["scene_buttons"]),
    )
    return structured, True


def replace_section(structured: dict, section: str, value) -> dict:
    """Return a deep-ish copy of `structured` with one section replaced.

    `section` uses dot-notation, e.g. "room", "screens.media", "screensaver".
    """
    out = _shallow_copy_nested(structured)
    keys = section.split(".")
    cursor = out
    for key in keys[:-1]:
        nxt = cursor.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
        cursor[key] = dict(nxt)
        cursor = cursor[key]
    cursor[keys[-1]] = value
    return out


def _shallow_copy_nested(d: dict) -> dict:
    """Shallow copy that re-creates one level of nested dicts so mutation
    of a section doesn't bleed into the caller's reference."""
    out: dict = {}
    for k, v in d.items():
        out[k] = dict(v) if isinstance(v, dict) else (list(v) if isinstance(v, list) else v)
    return out
