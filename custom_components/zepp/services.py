"""Services for the Zepp integration."""
from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant, ServiceCall

from .const import CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS, DOMAIN

_LOGGER = logging.getLogger(__name__)

SERVICE_SYNC_HISTORY = "sync_history"


async def async_register_services(hass: HomeAssistant) -> None:
    """Register the history sync service."""
    if hass.services.has_service(DOMAIN, SERVICE_SYNC_HISTORY):
        return

    async def handle_sync_history(call: ServiceCall) -> None:
        days = int(call.data.get("days", DEFAULT_BACKFILL_DAYS))
        for entry_info in (hass.data.get(DOMAIN) or {}).values():
            coordinator = entry_info.get("coordinator") if isinstance(entry_info, dict) else None
            if coordinator is None:
                continue
            entry_days = int(
                coordinator.entry.options.get(CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS)
            )
            _LOGGER.info("Starting Zepp history import for %s days", days or entry_days)
            hass.async_create_task(coordinator.async_backfill(days or entry_days))

    hass.services.async_register(DOMAIN, SERVICE_SYNC_HISTORY, handle_sync_history)
