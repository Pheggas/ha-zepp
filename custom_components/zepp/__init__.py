"""Zepp (Amazfit) integration setup."""
from __future__ import annotations

import logging
from datetime import timedelta

import homeassistant.helpers.config_validation as cv
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BACKFILL_DAYS,
    CONF_DETAILED_STATS,
    CONF_SCAN_INTERVAL,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .coordinator import ZeppCoordinator
from .services import async_register_services

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BUTTON]

STORAGE_VERSION = 1
# A full backfill is re-run when the stored one is older than this. Zepp does
# re-sync old days occasionally, so history is not write-once.
BACKFILL_MAX_AGE = timedelta(days=7)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register the services once per instance."""
    hass.data.setdefault(DOMAIN, {})
    await async_register_services(hass)
    return True


def _storage(hass: HomeAssistant, entry: ConfigEntry) -> Store:
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one Zepp account."""
    hass.data.setdefault(DOMAIN, {})

    options = {
        CONF_SCAN_INTERVAL: int(entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)),
        CONF_DETAILED_STATS: bool(entry.options.get(CONF_DETAILED_STATS, True)),
        CONF_BACKFILL_DAYS: int(entry.options.get(CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS)),
    }

    coordinator = ZeppCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {
        "data": entry.data,
        "coordinator": coordinator,
    }

    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Resolve entity ids only once the platforms exist, so the statistics this
    # account writes land on the entities that were actually created. The
    # registry is filled asynchronously, so this retries until it is populated.
    hass.async_create_task(coordinator.async_wait_for_statistic_ids())

    task = hass.async_create_task(_async_backfill_if_due(hass, entry, coordinator, options))
    hass.data[DOMAIN][entry.entry_id]["backfill_task"] = task
    return True


async def _async_backfill_if_due(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: ZeppCoordinator,
    options: dict,
) -> None:
    """Import history on first run, and refresh it when it is stale."""
    store = _storage(hass, entry)
    stored = await store.async_load() or {}
    days = int(options.get(CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS))

    last = stored.get("last_backfill")
    due = True
    if last:
        try:
            moment = dt_util.parse_datetime(last)
        except ValueError:
            moment = None
        if moment is not None and dt_util.utcnow() - moment < BACKFILL_MAX_AGE:
            due = False

    if not due:
        _LOGGER.debug("Zepp history was imported recently (%s), skipping backfill", last)
        return

    try:
        imported = await coordinator.async_backfill(days)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Zepp historical import failed: %s", err)
        return

    await store.async_save(
        {"last_backfill": dt_util.utcnow().isoformat(), "days": imported}
    )
    _LOGGER.info("Imported %s days of Zepp history", imported)


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when the options change (cadence, detail level)."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the platform and cancel anything still running."""
    info = hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}
    task = info.get("backfill_task")
    if task and not task.done():
        task.cancel()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok
