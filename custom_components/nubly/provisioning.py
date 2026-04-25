"""MQTT provisioning detection (phase 1: detection only).

Determines whether the current Home Assistant installation has a Supervisor
with the Mosquitto add-on installed. No credentials are generated, no add-on
configuration is modified, and nothing is published over MQTT — this module
only inspects the environment so a future phase 2 can decide what to do.
"""

import logging
import os

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

SUPERVISOR_TOKEN_ENV = "SUPERVISOR_TOKEN"
MOSQUITTO_INFO_URL = "http://supervisor/addons/core_mosquitto/info"
REQUEST_TIMEOUT_SECONDS = 5


async def async_check_provisioning_support(hass: HomeAssistant) -> dict:
    """Probe Supervisor for Mosquitto add-on availability.

    Returns a dict: {"supervisor": bool, "mosquitto": bool, "supported": bool}.
    Logs the conclusion. Never raises.
    """
    result = {"supervisor": False, "mosquitto": False, "supported": False}

    token = os.environ.get(SUPERVISOR_TOKEN_ENV)
    if not token:
        _LOGGER.warning("NUBLY HA: supervisor detected = false")
        _LOGGER.warning("NUBLY HA: mosquitto add-on detected = false")
        _LOGGER.warning(
            "NUBLY HA: Automatic MQTT provisioning is not supported yet"
        )
        return result

    result["supervisor"] = True
    _LOGGER.warning("NUBLY HA: supervisor detected = true")

    session = async_get_clientsession(hass)
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)

    try:
        async with session.get(
            MOSQUITTO_INFO_URL,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        ) as resp:
            if resp.status == 200:
                body = await resp.json()
                data = body.get("data") if isinstance(body, dict) else None
                # Supervisor returns data.version when the add-on is installed,
                # regardless of whether it's currently running.
                if isinstance(data, dict) and data.get("version"):
                    result["mosquitto"] = True
            elif resp.status == 404:
                # Add-on slug exists but isn't installed on this host.
                result["mosquitto"] = False
            else:
                _LOGGER.warning(
                    "NUBLY HA: supervisor returned HTTP %s for mosquitto info",
                    resp.status,
                )
    except Exception:
        _LOGGER.exception("NUBLY HA: supervisor probe failed")
        _LOGGER.warning(
            "NUBLY HA: Automatic MQTT provisioning is not supported yet"
        )
        return result

    _LOGGER.warning(
        "NUBLY HA: mosquitto add-on detected = %s", result["mosquitto"]
    )

    if result["mosquitto"]:
        result["supported"] = True
        _LOGGER.warning(
            "NUBLY HA: automatic MQTT provisioning will be supported (phase 2 not implemented)"
        )
    else:
        _LOGGER.warning(
            "NUBLY HA: Automatic MQTT provisioning is not supported yet"
        )

    return result
