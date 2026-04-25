"""MQTT provisioning helpers.

Phase 1 (detection): probes Supervisor / Mosquitto availability.
Phase 2/3 (this module): orchestrates the full onboarding sequence — generate
credentials, register the user with the Mosquitto add-on via Supervisor,
restart Mosquitto, wait for HA's MQTT client to reconnect, then POST the
credentials to the ESP32 /provision endpoint.

Passwords are generated with `secrets.token_urlsafe`, kept in local variables
only for the duration of the flow, never logged, never stored in any config
entry, never published over MQTT.
"""

import asyncio
import logging
import os
import secrets

import aiohttp

from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

SUPERVISOR_TOKEN_ENV = "SUPERVISOR_TOKEN"
SUPERVISOR_BASE = "http://supervisor"
MOSQUITTO_INFO_URL = f"{SUPERVISOR_BASE}/addons/core_mosquitto/info"
MOSQUITTO_OPTIONS_URL = f"{SUPERVISOR_BASE}/addons/core_mosquitto/options"
MOSQUITTO_RESTART_URL = f"{SUPERVISOR_BASE}/addons/core_mosquitto/restart"

REQUEST_TIMEOUT_SECONDS = 5
SUPERVISOR_WRITE_TIMEOUT_SECONDS = 30
PROVISION_PORT = 80
PROVISION_TIMEOUT_SECONDS = 10
MQTT_RECONNECT_TIMEOUT_SECONDS = 15
DEFAULT_BROKER_HOST = "homeassistant.local"
DEFAULT_BROKER_PORT = 1883


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
                if isinstance(data, dict) and data.get("version"):
                    result["mosquitto"] = True
            elif resp.status == 404:
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
            "NUBLY HA: automatic MQTT provisioning will be supported"
        )
    else:
        _LOGGER.warning(
            "NUBLY HA: Automatic MQTT provisioning is not supported yet"
        )

    return result


async def async_provision_device(
    hass: HomeAssistant, host: str, device_id: str
) -> str | None:
    """Run the full onboarding provisioning flow.

    Order of operations:
      1. Generate credentials.
      2. Add the user to the Mosquitto add-on options.
      3. Restart Mosquitto (once).
      4. Wait for HA's MQTT client to reconnect.
      5. POST the credentials to the ESP32 /provision endpoint.

    Returns None on success, or a translation key for the failing step.
    """
    token = os.environ.get(SUPERVISOR_TOKEN_ENV)
    if not token:
        _LOGGER.warning(
            "NUBLY HA: supervisor not available — cannot provision automatically"
        )
        return "supervisor_unavailable"

    username = f"nubly_{device_id}"
    password = secrets.token_urlsafe(32)

    if not await _async_add_mosquitto_user(hass, token, username, password):
        return "mosquitto_user_add_failed"

    _LOGGER.warning("NUBLY HA: restarting Mosquitto for credential provisioning")
    if not await _async_restart_mosquitto(hass, token):
        return "mosquitto_restart_failed"

    _LOGGER.warning("NUBLY HA: waiting for MQTT reconnect")
    if not await _async_wait_for_mqtt(hass):
        return "mqtt_reconnect_timeout"
    _LOGGER.warning("NUBLY HA: MQTT reconnected")

    if not await _async_post_provision(hass, host, device_id, username, password):
        return "provisioning_failed"

    return None


async def _async_add_mosquitto_user(
    hass: HomeAssistant, token: str, username: str, password: str
) -> bool:
    """Append (or replace) a login in Mosquitto add-on options.

    Returns True if Supervisor accepts the new options, False otherwise.
    """
    session = async_get_clientsession(hass)
    headers = {"Authorization": f"Bearer {token}"}
    read_timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
    write_timeout = aiohttp.ClientTimeout(total=SUPERVISOR_WRITE_TIMEOUT_SECONDS)

    try:
        async with session.get(
            MOSQUITTO_INFO_URL, headers=headers, timeout=read_timeout
        ) as resp:
            if resp.status != 200:
                _LOGGER.warning(
                    "NUBLY HA: mosquitto info returned HTTP %s", resp.status
                )
                return False
            body = await resp.json()
    except Exception:
        _LOGGER.exception("NUBLY HA: failed to fetch mosquitto info")
        return False

    data = body.get("data") if isinstance(body, dict) else None
    options = dict((data or {}).get("options") or {})
    logins = [
        login
        for login in (options.get("logins") or [])
        if isinstance(login, dict) and login.get("username") != username
    ]
    logins.append({"username": username, "password": password})
    options["logins"] = logins

    try:
        async with session.post(
            MOSQUITTO_OPTIONS_URL,
            headers=headers,
            json={"options": options},
            timeout=write_timeout,
        ) as resp:
            if resp.status != 200:
                _LOGGER.warning(
                    "NUBLY HA: mosquitto options POST returned HTTP %s",
                    resp.status,
                )
                return False
    except Exception:
        _LOGGER.exception("NUBLY HA: failed to update mosquitto options")
        return False

    _LOGGER.warning("NUBLY HA: added MQTT user %s to Mosquitto", username)
    return True


async def _async_restart_mosquitto(hass: HomeAssistant, token: str) -> bool:
    session = async_get_clientsession(hass)
    headers = {"Authorization": f"Bearer {token}"}
    timeout = aiohttp.ClientTimeout(total=SUPERVISOR_WRITE_TIMEOUT_SECONDS)

    try:
        async with session.post(
            MOSQUITTO_RESTART_URL, headers=headers, timeout=timeout
        ) as resp:
            if resp.status != 200:
                _LOGGER.warning(
                    "NUBLY HA: mosquitto restart returned HTTP %s", resp.status
                )
                return False
    except Exception:
        _LOGGER.exception("NUBLY HA: failed to restart mosquitto")
        return False
    return True


async def _async_wait_for_mqtt(hass: HomeAssistant) -> bool:
    wait_fn = getattr(mqtt, "async_wait_for_mqtt_client", None)
    if wait_fn is None:
        _LOGGER.warning(
            "NUBLY HA: async_wait_for_mqtt_client not available; sleeping briefly"
        )
        await asyncio.sleep(5)
        return True

    try:
        await asyncio.wait_for(
            wait_fn(hass), timeout=MQTT_RECONNECT_TIMEOUT_SECONDS
        )
        return True
    except asyncio.TimeoutError:
        _LOGGER.warning(
            "NUBLY HA: MQTT did not reconnect within %ss",
            MQTT_RECONNECT_TIMEOUT_SECONDS,
        )
        return False
    except Exception:
        _LOGGER.exception("NUBLY HA: MQTT wait raised unexpectedly")
        return False


async def _async_post_provision(
    hass: HomeAssistant,
    host: str,
    device_id: str,
    username: str,
    password: str,
) -> bool:
    url = f"http://{host}:{PROVISION_PORT}/provision"
    _LOGGER.warning("NUBLY HA: provisioning device at %s", url)

    payload = {
        "mqtt_host": _get_broker_host(hass),
        "mqtt_port": _get_broker_port(hass),
        "mqtt_username": username,
        "mqtt_password": password,
        "device_id": device_id,
    }
    _LOGGER.warning(
        "NUBLY HA: provisioning payload prepared for device_id = %s", device_id
    )

    session = async_get_clientsession(hass)
    timeout = aiohttp.ClientTimeout(total=PROVISION_TIMEOUT_SECONDS)

    try:
        async with session.post(url, json=payload, timeout=timeout) as resp:
            _LOGGER.warning(
                "NUBLY HA: provisioning response status = %s", resp.status
            )
            if resp.status == 200:
                _LOGGER.warning("NUBLY HA: provisioning succeeded")
                return True
            _LOGGER.warning(
                "NUBLY HA: provisioning failed (HTTP %s)", resp.status
            )
            return False
    except asyncio.TimeoutError:
        _LOGGER.warning("NUBLY HA: provisioning failed (timeout)")
        return False
    except aiohttp.ClientError:
        _LOGGER.warning("NUBLY HA: provisioning failed (connection error)")
        return False
    except Exception:
        _LOGGER.exception("NUBLY HA: provisioning failed (unexpected error)")
        return False


def _get_broker_host(hass: HomeAssistant) -> str:
    entries = hass.config_entries.async_entries("mqtt")
    if entries:
        return entries[0].data.get("broker") or DEFAULT_BROKER_HOST
    return DEFAULT_BROKER_HOST


def _get_broker_port(hass: HomeAssistant) -> int:
    entries = hass.config_entries.async_entries("mqtt")
    if entries:
        try:
            return int(entries[0].data.get("port") or DEFAULT_BROKER_PORT)
        except (TypeError, ValueError):
            return DEFAULT_BROKER_PORT
    return DEFAULT_BROKER_PORT
