"""HTTP views for the Nubly integration."""

import hashlib
import logging

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import CONF_DEVICE_ID, CONF_MEDIA_ENTITY, DOMAIN

_LOGGER = logging.getLogger(__name__)


class NublyCoverArtView(HomeAssistantView):
    """Serve the current media artwork for a Nubly device.

    No auth: scoped by the device's hardware id, which the ESP32 already
    knows. HA performs the media_player lookup in-process, so no HA token
    is ever exposed to the device.
    """

    url = "/api/nubly/{device_id}/cover_art"
    name = "api:nubly:cover_art"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, device_id: str) -> web.Response:
        _LOGGER.warning(
            "NUBLY HA: cover art requested for device_id = %s", device_id
        )

        entry = _find_entry_by_device_id(self.hass, device_id)
        if entry is None:
            _LOGGER.warning("NUBLY HA: cover art HTTP status = 404 (unknown device)")
            return web.Response(status=404, text="unknown device")

        media_entity = entry.data.get(CONF_MEDIA_ENTITY)
        _LOGGER.warning("NUBLY HA: media entity = %s", media_entity)
        if not media_entity:
            _LOGGER.warning(
                "NUBLY HA: cover art HTTP status = 404 (no media entity)"
            )
            return web.Response(status=404, text="no media entity configured")

        state = self.hass.states.get(media_entity)
        attrs = state.attributes if state is not None else {}
        entity_picture = attrs.get("entity_picture")
        media_image_url = attrs.get("media_image_url")
        _LOGGER.warning("NUBLY HA: entity_picture = %s", entity_picture)
        _LOGGER.warning(
            "NUBLY HA: resolved artwork url = %s",
            media_image_url or entity_picture,
        )

        etag_source = "|".join(
            str(attrs.get(k, ""))
            for k in ("entity_picture", "media_title", "media_content_id")
        )
        etag = (
            hashlib.sha1(etag_source.encode("utf-8")).hexdigest()[:16]
            if etag_source.strip("|")
            else ""
        )

        if etag and request.headers.get("If-None-Match") == etag:
            _LOGGER.warning("NUBLY HA: cover art HTTP status = 304 (not modified)")
            return web.Response(status=304, headers={"ETag": etag})

        component = self.hass.data.get("media_player")
        if component is None:
            _LOGGER.warning(
                "NUBLY HA: cover art HTTP status = 503 (media_player unavailable)"
            )
            return web.Response(status=503, text="media_player unavailable")

        player = component.get_entity(media_entity)
        if player is None:
            _LOGGER.warning(
                "NUBLY HA: cover art HTTP status = 404 (entity not found)"
            )
            return web.Response(status=404, text="entity not found")

        try:
            image = await player.async_get_media_image()
        except Exception:
            _LOGGER.exception("NUBLY HA: cover art HTTP status = 502 (fetch failed)")
            return web.Response(status=502, text="fetch failed")

        if not image or not image[0]:
            _LOGGER.warning("NUBLY HA: cover art HTTP status = 404 (no image)")
            return web.Response(status=404, text="no image")

        image_bytes, content_type = image
        content_type = content_type or "image/jpeg"

        headers = {"Cache-Control": "no-cache, max-age=0"}
        if etag:
            headers["ETag"] = etag

        _LOGGER.warning("NUBLY HA: cover art HTTP status = 200")
        _LOGGER.warning("NUBLY HA: response content type = %s", content_type)
        _LOGGER.warning("NUBLY HA: response bytes = %d", len(image_bytes))

        return web.Response(
            body=image_bytes,
            content_type=content_type,
            headers=headers,
        )


def _find_entry_by_device_id(hass: HomeAssistant, device_id: str):
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(CONF_DEVICE_ID) == device_id:
            return entry
    return None
