"""HTTP views for the Nubly integration.

Cover art proxy: fetches upstream artwork (from a media_player or a
direct URL), decodes it server-side, normalizes to RGB JPEG, and resizes
to fit within the requesting board's capability bounds — never larger
than the source. Output is always JPEG when conversion succeeds.
"""

from __future__ import annotations

import hashlib
import io
import logging

from aiohttp import ClientError, web

from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_DEVICE_ID, CONF_MEDIA_ENTITY, CONF_MODEL, DOMAIN
from .device_data import NublyDeviceData, get_attr
from .firmware import cover_art_bounds, normalize_board

_LOGGER = logging.getLogger(__name__)

# Magic bytes used to detect the upstream format up-front. Pillow is the
# authoritative decoder; this is only for logging and a fast reject.
_MAGIC = {
    "jpeg": (b"\xff\xd8",),
    "png": (b"\x89PNG\r\n\x1a\n",),
    "gif": (b"GIF87a", b"GIF89a"),
    "webp": None,  # RIFF....WEBP — checked specially.
    "bmp": (b"BM",),
}

_SUPPORTED_FORMATS = {"jpeg", "png", "webp", "gif", "bmp"}
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

        # Resolve board for output sizing.
        board = self._resolve_board(entry, device_id)
        max_w, max_h, is_default = cover_art_bounds(board)
        if is_default:
            _LOGGER.warning(
                "NUBLY HA: cover art board unknown for device=%s "
                "(reported=%s) — using conservative bounds %dx%d",
                device_id,
                board,
                max_w,
                max_h,
            )
        _LOGGER.debug(
            "NUBLY HA: cover art board=%s bounds=%dx%d device=%s",
            board,
            max_w,
            max_h,
            device_id,
        )

        media_entity = (
            query_entity
            or (entry.data.get(CONF_MEDIA_ENTITY) if entry else None)
        )
        _LOGGER.debug("NUBLY HA: cover art resolved entity=%s", media_entity)

        # ETag from upstream picture + title + content_id + board + cache.
        # Including bounds means a device whose board changes (HW swap)
        # invalidates cached entries automatically.
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
        etag_payload = f"{source_hint}|{board}|{max_w}x{max_h}|{query_cache or ''}"
        etag = (
            hashlib.sha1(etag_payload.encode("utf-8")).hexdigest()[:16]
            if source_hint.strip("|")
            else ""
        )

        if etag and request.headers.get("If-None-Match") == etag:
            _LOGGER.debug(
                "NUBLY HA: cover art 304 not modified device=%s", device_id
            )
            return web.Response(status=304, headers={"ETag": etag})

        # Fetch raw upstream bytes.
        try:
            if query_pic:
                image_bytes, upstream_ct, upstream_status = (
                    await self._fetch_direct(query_pic)
                )
                _LOGGER.debug(
                    "NUBLY HA: upstream url=%s status=%s content_type=%s bytes=%d",
                    query_pic,
                    upstream_status,
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

        # Detect/validate the source format. Content-Type is logged only,
        # never trusted. Pillow's open() is the real validator.
        detected = _detect_format(image_bytes)
        _LOGGER.debug(
            "NUBLY HA: cover art detected_format=%s upstream_content_type=%s",
            detected,
            upstream_ct,
        )
        if detected not in _SUPPORTED_FORMATS:
            _LOGGER.warning(
                "NUBLY HA: unsupported source image format detected=%s "
                "upstream_content_type=%s first8=%s",
                detected,
                upstream_ct,
                image_bytes[:8].hex(),
            )
            return _err(415, f"unsupported image format: {detected}")

        try:
            jpeg_bytes, src_size, out_size = await self.hass.async_add_executor_job(
                _normalize_to_jpeg, image_bytes, max_w, max_h
            )
        except _UnsupportedFormatError as err:
            _LOGGER.warning(
                "NUBLY HA: Pillow refused the source image (%s)", err
            )
            return _err(415, "unsupported image format")
        except Exception:
            _LOGGER.exception(
                "NUBLY HA: cover art decode/encode failed (source_bytes=%d)",
                len(image_bytes),
            )
            return _err(500, "image conversion failed")

        if not jpeg_bytes.startswith(_JPEG_MAGIC):
            _LOGGER.error(
                "NUBLY HA: refusing to serve non-JPEG bytes (first4=%s)",
                jpeg_bytes[:4].hex(),
            )
            return _err(500, "image conversion produced non-JPEG output")

        headers = {"Cache-Control": "no-cache, max-age=0"}
        if etag:
            headers["ETag"] = etag

        _LOGGER.debug(
            "NUBLY HA: cover art returning device=%s board=%s "
            "source_size=%sx%s output_size=%sx%s jpeg_bytes=%d",
            device_id,
            board,
            src_size[0],
            src_size[1],
            out_size[0],
            out_size[1],
            len(jpeg_bytes),
        )

        return web.Response(
            body=jpeg_bytes,
            content_type="image/jpeg",
            headers=headers,
        )

    def _resolve_board(
        self, entry: ConfigEntry | None, device_id: str
    ) -> str | None:
        """Best-effort board id for the requesting device.

        Checks live telemetry first (attributes.board/device_type/model),
        then the value captured at config-entry creation.
        """
        bucket = self.hass.data.get(DOMAIN, {}).get(
            entry.entry_id if entry else None
        )
        if isinstance(bucket, dict):
            device_data = bucket.get("device_data")
            if isinstance(device_data, NublyDeviceData):
                raw = get_attr(
                    device_data.attributes,
                    "board",
                    "device_type",
                    "model",
                )
                if isinstance(raw, str) and raw:
                    return normalize_board(raw)
        if entry is not None:
            configured = entry.data.get(CONF_MODEL)
            if isinstance(configured, str):
                return normalize_board(configured)
        return None

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

    async def _fetch_direct(
        self, url: str
    ) -> tuple[bytes, str | None, int]:
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
                return data, content_type, resp.status
        except ClientError as err:
            raise _CoverArtError(502, f"upstream error: {err}") from err


class _CoverArtError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class _UnsupportedFormatError(Exception):
    pass


def _err(status: int, message: str) -> web.Response:
    _LOGGER.debug("NUBLY HA: cover art HTTP status=%s msg=%s", status, message)
    return web.Response(status=status, text=message)


def _detect_format(data: bytes) -> str:
    """Detect image format from leading bytes."""
    if data.startswith(_JPEG_MAGIC):
        return "jpeg"
    if data.startswith(_MAGIC["png"][0]):
        return "png"
    if len(data) >= 6 and data[:6] in _MAGIC["gif"]:
        return "gif"
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
        return "webp"
    if data[:2] == _MAGIC["bmp"][0]:
        return "bmp"
    return "unknown"


def _normalize_to_jpeg(
    image_bytes: bytes, max_w: int, max_h: int
) -> tuple[bytes, tuple[int, int], tuple[int, int]]:
    """Decode → flatten → resize-to-fit → encode JPEG.

    Returns (jpeg_bytes, source_size, output_size). Never upscales: if the
    source is smaller than the bounds in both dimensions it is returned at
    its native size (still re-encoded as JPEG).

    Runs in an executor — Pillow's decode/encode is CPU-bound.
    """
    # Local import: Pillow is declared in manifest.json `requirements`.
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError as err:  # pragma: no cover — manifest installs it
        raise RuntimeError(f"Pillow not available: {err}") from err

    try:
        img = Image.open(io.BytesIO(image_bytes))
    except UnidentifiedImageError as err:
        raise _UnsupportedFormatError(str(err)) from err

    with img:
        src_size = img.size  # (width, height)

        # Flatten transparency against a solid white background; JPEG has
        # no alpha channel.
        if img.mode in ("RGBA", "LA") or (
            img.mode == "P" and "transparency" in img.info
        ):
            background = Image.new("RGB", src_size, (255, 255, 255))
            rgba = img.convert("RGBA")
            background.paste(rgba, mask=rgba.split()[-1])
            converted = background
        else:
            converted = img.convert("RGB")

        # Downscale only — preserve aspect ratio. `thumbnail` is a no-op
        # when the image is already within bounds.
        converted.thumbnail((max_w, max_h), Image.LANCZOS)
        out_size = converted.size

        out = io.BytesIO()
        converted.save(
            out, format="JPEG", quality=_JPEG_QUALITY, optimize=True
        )
        return out.getvalue(), src_size, out_size


def _find_entry_by_device_id(hass: HomeAssistant, device_id: str):
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(CONF_DEVICE_ID) == device_id:
            return entry
    return None
