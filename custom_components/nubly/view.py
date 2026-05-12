"""HTTP views for the Nubly integration.

The cover art proxy always returns real JPEG bytes. Upstream PNG (or any
non-JPEG) is decoded and re-encoded server-side via Pillow. Content-Type
is set to image/jpeg only when the response body is actually JPEG.
"""

from __future__ import annotations

import hashlib
import io
import logging

from aiohttp import ClientError, web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_DEVICE_ID, CONF_MEDIA_ENTITY, DOMAIN

_LOGGER = logging.getLogger(__name__)

# Magic bytes used to detect the actual image format regardless of the
# upstream Content-Type header.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8"

_JPEG_QUALITY = 85
_FETCH_TIMEOUT_SECONDS = 10


class NublyCoverArtView(HomeAssistantView):
    """Serve the current media artwork for a Nubly device as JPEG.

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
        # Query params (all optional, all preserved):
        #   entity=<entity_id>   -- override which media_player to read
        #   pic=<absolute url>   -- direct fetch URL, bypasses media_player
        #   cache=<token>        -- cache-busting token, no server effect
        query_entity = request.query.get("entity")
        query_pic = request.query.get("pic")
        query_cache = request.query.get("cache") or request.query.get("v")

        _LOGGER.debug(
            "NUBLY HA: cover art requested device_id=%s entity=%s pic=%s cache=%s",
            device_id,
            query_entity,
            query_pic,
            query_cache,
        )

        entry = _find_entry_by_device_id(self.hass, device_id)
        if entry is None and not query_pic:
            return _err(404, "unknown device")

        media_entity = (
            query_entity
            or (entry.data.get(CONF_MEDIA_ENTITY) if entry else None)
        )
        _LOGGER.debug("NUBLY HA: cover art resolved entity=%s", media_entity)

        # ETag computed from upstream picture+title+content_id when available;
        # for ?pic= direct fetches we hash the URL itself.
        attrs: dict = {}
        upstream_picture: str | None = None
        if media_entity:
            state = self.hass.states.get(media_entity)
            attrs = state.attributes if state is not None else {}
            upstream_picture = attrs.get("media_image_url") or attrs.get(
                "entity_picture"
            )

        source_hint = (
            query_pic
            or "|".join(
                str(attrs.get(k, ""))
                for k in ("entity_picture", "media_title", "media_content_id")
            )
        )
        etag = (
            hashlib.sha1(
                f"{source_hint}|{query_cache or ''}".encode("utf-8")
            ).hexdigest()[:16]
            if source_hint.strip("|")
            else ""
        )

        if etag and request.headers.get("If-None-Match") == etag:
            _LOGGER.debug(
                "NUBLY HA: cover art 304 not modified device=%s", device_id
            )
            return web.Response(status=304, headers={"ETag": etag})

        # Fetch raw image bytes either via HA's media_player helper or via
        # a direct HTTP fetch when ?pic= is provided.
        try:
            if query_pic:
                image_bytes, upstream_ct = await self._fetch_direct(query_pic)
                _LOGGER.debug(
                    "NUBLY HA: upstream picture url=%s content_type=%s bytes=%d",
                    query_pic,
                    upstream_ct,
                    len(image_bytes) if image_bytes else 0,
                )
            else:
                if not media_entity:
                    return _err(404, "no media entity configured")
                image_bytes, upstream_ct = await self._fetch_via_player(
                    media_entity
                )
                _LOGGER.debug(
                    "NUBLY HA: upstream picture url=%s content_type=%s bytes=%d",
                    upstream_picture,
                    upstream_ct,
                    len(image_bytes) if image_bytes else 0,
                )
        except _CoverArtError as err:
            return _err(err.status, err.message)
        except Exception:
            _LOGGER.exception("NUBLY HA: cover art unexpected fetch error")
            return _err(502, "fetch failed")

        if not image_bytes:
            return _err(404, "no image")

        # Detect the real format from magic bytes — never trust Content-Type.
        source_format = _detect_format(image_bytes)
        _LOGGER.debug(
            "NUBLY HA: cover art source format detected=%s", source_format
        )

        if source_format == "jpeg":
            jpeg_bytes = image_bytes
        else:
            try:
                jpeg_bytes = await self.hass.async_add_executor_job(
                    _to_jpeg, image_bytes
                )
            except Exception:
                _LOGGER.exception(
                    "NUBLY HA: cover art conversion to JPEG failed "
                    "(source=%s, %d bytes)",
                    source_format,
                    len(image_bytes),
                )
                return _err(502, "image conversion failed")

        # Final safety check: never return non-JPEG bytes with image/jpeg.
        if not jpeg_bytes.startswith(_JPEG_MAGIC):
            _LOGGER.error(
                "NUBLY HA: refusing to serve non-JPEG bytes (first4=%s) as image/jpeg",
                jpeg_bytes[:4].hex(),
            )
            return _err(502, "image conversion produced non-JPEG output")

        headers = {"Cache-Control": "no-cache, max-age=0"}
        if etag:
            headers["ETag"] = etag

        _LOGGER.debug(
            "NUBLY HA: cover art returning format=jpeg bytes=%d device=%s",
            len(jpeg_bytes),
            device_id,
        )

        return web.Response(
            body=jpeg_bytes,
            content_type="image/jpeg",
            headers=headers,
        )

    async def _fetch_via_player(
        self, media_entity: str
    ) -> tuple[bytes, str | None]:
        component = self.hass.data.get("media_player")
        if component is None:
            raise _CoverArtError(503, "media_player unavailable")
        player = component.get_entity(media_entity)
        if player is None:
            raise _CoverArtError(404, "entity not found")
        try:
            image = await player.async_get_media_image()
        except Exception:
            _LOGGER.exception(
                "NUBLY HA: async_get_media_image raised for %s", media_entity
            )
            raise _CoverArtError(502, "fetch failed")
        if not image or not image[0]:
            raise _CoverArtError(404, "no image")
        return image[0], (image[1] or None)

    async def _fetch_direct(self, url: str) -> tuple[bytes, str | None]:
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                url, timeout=_FETCH_TIMEOUT_SECONDS
            ) as resp:
                if resp.status != 200:
                    raise _CoverArtError(
                        502, f"upstream HTTP {resp.status}"
                    )
                content_type = resp.headers.get("Content-Type")
                data = await resp.read()
        except ClientError as err:
            raise _CoverArtError(502, f"upstream error: {err}") from err
        return data, content_type


class _CoverArtError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _err(status: int, message: str) -> web.Response:
    _LOGGER.debug("NUBLY HA: cover art HTTP status=%s msg=%s", status, message)
    return web.Response(status=status, text=message)


def _detect_format(data: bytes) -> str:
    """Detect image format from leading bytes."""
    if data.startswith(_JPEG_MAGIC):
        return "jpeg"
    if data.startswith(_PNG_MAGIC):
        return "png"
    if len(data) >= 6 and data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return "unknown"


def _to_jpeg(image_bytes: bytes) -> bytes:
    """Decode arbitrary image bytes via Pillow and re-encode as JPEG.

    Runs in an executor — Pillow's decode/encode is CPU-bound and would
    otherwise block the asyncio event loop.
    """
    # Local import: Pillow is a runtime dependency declared in manifest.json,
    # and importing at module top would slow integration import in the rare
    # case Pillow isn't installed yet.
    from PIL import Image

    with Image.open(io.BytesIO(image_bytes)) as img:
        # JPEG cannot store an alpha channel — flatten onto white when needed.
        if img.mode in ("RGBA", "LA") or (
            img.mode == "P" and "transparency" in img.info
        ):
            background = Image.new("RGB", img.size, (255, 255, 255))
            rgba = img.convert("RGBA")
            background.paste(rgba, mask=rgba.split()[-1])
            converted = background
        else:
            converted = img.convert("RGB")

        out = io.BytesIO()
        converted.save(
            out, format="JPEG", quality=_JPEG_QUALITY, optimize=True
        )
        return out.getvalue()


def _find_entry_by_device_id(hass: HomeAssistant, device_id: str):
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(CONF_DEVICE_ID) == device_id:
            return entry
    return None
