"""Buttons for the Zepp integration."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_USERID, DOMAIN
from .coordinator import ZeppCoordinator
from .sensor import _device_info

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the sync buttons."""
    coordinator: ZeppCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    devices = coordinator.devices or [
        {"device_id": str(entry.data.get(CONF_USERID)), "device_name": "Amazfit Watch"}
    ]

    buttons: list[ButtonEntity] = []
    seen: set[str] = set()
    for device in devices:
        device_id = str(device.get("device_id"))
        if device_id in seen:
            continue
        seen.add(device_id)
        info = _device_info(device)
        buttons.append(ZeppSyncButton(coordinator, device_id, info))
        buttons.append(ZeppBackfillButton(coordinator, device_id, info, entry))

    async_add_entities(buttons)


class ZeppSyncButton(ButtonEntity):
    """Ask the coordinator to poll the cloud right now."""

    _attr_has_entity_name = True
    _attr_name = "Sync Now"
    _attr_icon = "mdi:sync"
    _attr_device_class = ButtonDeviceClass.UPDATE

    def __init__(
        self,
        coordinator: ZeppCoordinator,
        device_id: str,
        device_info: DeviceInfo,
    ) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{device_id}_sync_now"
        self._attr_device_info = device_info

    async def async_press(self) -> None:
        await self._coordinator.async_request_refresh()


class ZeppBackfillButton(ButtonEntity):
    """Import the whole history the account still holds."""

    _attr_has_entity_name = True
    _attr_name = "Sync Full History"
    _attr_icon = "mdi:database-sync"
    _attr_device_class = ButtonDeviceClass.UPDATE
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: ZeppCoordinator,
        device_id: str,
        device_info: DeviceInfo,
        entry: ConfigEntry,
    ) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{device_id}_sync_history_365"
        self._attr_device_info = device_info

    async def async_press(self) -> None:
        days = int(self._entry.options.get("backfill_days", 365))
        self._coordinator.hass.async_create_task(self._coordinator.async_backfill(days))
