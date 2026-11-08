"""Diagnostics for the Zepp integration.

This is the file to look at when the data looks wrong: it reports which source
returned what, how the binary activity blob was interpreted, and whether the
5-minute writer is enabled.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_APPTOKEN, CONF_EMAIL, CONF_PASSWORD, DOMAIN

TO_REDACT = {
    CONF_APPTOKEN,
    CONF_PASSWORD,
    CONF_EMAIL,
    "auth_key",
    "bt_mac",
    "device_mac",
    "device_sn",
    "macAddress",
    "sn",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return the integration state with secrets removed."""
    info = (hass.data.get(DOMAIN) or {}).get(entry.entry_id) or {}
    coordinator = info.get("coordinator")
    data = dict(coordinator.data) if coordinator and coordinator.data else {}

    metrics = {
        key: value
        for key, value in data.items()
        if key not in ("sources", "devices", "device_batteries")
    }

    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "config": {
            "region_host": coordinator.client.host if coordinator else None,
            "scan_interval": str(coordinator.update_interval) if coordinator else None,
            "detailed_statistics": coordinator.detailed_stats if coordinator else None,
            "last_update_success": coordinator.last_update_success if coordinator else None,
            "last_error": coordinator.client.last_error if coordinator else None,
        },
        "activity_layout": data.get("layout"),
        "sources": data.get("sources"),
        "backfill": coordinator.backfill if coordinator else None,
        "metrics": metrics,
        "devices": [
            async_redact_data(dict(device), TO_REDACT)
            for device in (coordinator.devices if coordinator else [])
        ],
    }
