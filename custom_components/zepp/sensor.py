"""Sensor platform for the Zepp integration.

Every chartable value is produced by the coordinator at the resolution the
cloud actually holds (per minute), so the entities change as often as the data
does instead of once per daily upload.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import CONF_USERID, DOMAIN
from .coordinator import ZeppCoordinator
from .entities import SENSORS, SensorSpec

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the entities for every watch bound to the account."""
    coordinator: ZeppCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    devices = coordinator.devices or [
        {
            "device_id": str(entry.data.get(CONF_USERID)),
            "device_name": "Amazfit Watch",
            "device_mac": "",
            "device_sn": "",
            "firmware": "",
        }
    ]

    entities: list[SensorEntity] = []
    seen: set[str] = set()
    for device in devices:
        device_id = str(device.get("device_id"))
        if device_id in seen:
            # The same watch can be returned twice by the devices endpoint;
            # creating it twice doubles every entity.
            continue
        seen.add(device_id)
        info = _device_info(device)

        # Unique-id suffixes are kept from the previous release so existing
        # entities and their history survive the upgrade.
        entities.append(ZeppDeviceInfoSensor(device_id, "model", "Watch Model", "mdi:watch", device.get("device_name"), info, device, coordinator))
        entities.append(ZeppDeviceInfoSensor(device_id, "mac", "MAC Address", "mdi:bluetooth", device.get("device_mac") or "Unknown", info))
        entities.append(ZeppDeviceInfoSensor(device_id, "firmware", "Firmware Version", "mdi:cellphone-arrow-down", device.get("firmware") or "Unknown", info))
        entities.append(ZeppDeviceInfoSensor(device_id, "serial", "Serial Number", "mdi:barcode", device.get("device_sn") or "Unknown", info))

        if device.get("battery") is not None:
            entities.append(ZeppBatterySensor(coordinator, device_id, info))

        for spec in SENSORS:
            entities.append(ZeppSensor(coordinator, spec, device_id, info))

        entities.append(ZeppSyncStatusSensor(coordinator, device_id, info, hass))

    async_add_entities(entities)
    coordinator.async_resolve_statistic_ids()


def _device_info(device: dict[str, Any]) -> DeviceInfo:
    mac = str(device.get("device_mac") or "")
    connections = set()
    if mac and mac != "Unknown":
        connections.add((dr.CONNECTION_NETWORK_MAC, mac))
    return DeviceInfo(
        identifiers={(DOMAIN, str(device.get("device_id")))},
        name=str(device.get("device_name") or "Amazfit Watch"),
        manufacturer="Amazfit",
        model=str(device.get("device_name") or None),
        sw_version=str(device.get("firmware") or None),
        serial_number=str(device.get("device_sn") or None),
        connections=connections,
    )


def _state_class(value: str | None) -> SensorStateClass | None:
    if not value:
        return None
    try:
        return SensorStateClass(value)
    except ValueError:  # pragma: no cover
        return None


def _device_class(value: str | None) -> SensorDeviceClass | None:
    if not value:
        return None
    try:
        return SensorDeviceClass(value)
    except ValueError:  # pragma: no cover
        return None


class ZeppSensor(CoordinatorEntity[ZeppCoordinator], SensorEntity):
    """Generic sensor driven by a :class:`SensorSpec`."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ZeppCoordinator,
        spec: SensorSpec,
        device_id: str,
        device_info: DeviceInfo,
    ) -> None:
        super().__init__(coordinator)
        self.spec = spec
        self._attr_unique_id = f"{device_id}_{spec.key}"
        self._attr_name = spec.name
        self._attr_icon = spec.icon
        self._attr_native_unit_of_measurement = spec.unit
        self._attr_device_class = _device_class(spec.device_class)
        self._attr_state_class = _state_class(spec.state_class)
        self._attr_device_info = device_info

    @property
    def native_value(self) -> Any:
        value = self.coordinator.data.get(self.spec.value_key)
        if isinstance(value, float):
            return round(value, 2)
        if self.spec.device_class == "timestamp" and value is not None:
            return dt_util.parse_datetime(value) if isinstance(value, str) else value
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {}
        for key in self.spec.attributes:
            value = self.coordinator.data.get(key)
            if value is not None:
                attrs[key] = value
        # When this reading was measured, which is not the same as when it was
        # imported - the cloud can lag for days on some channels.
        measured = self.coordinator.data.get(f"{self.spec.value_key}_at")
        if measured is not None:
            attrs["measured_at"] = measured
        return attrs

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None


class ZeppDeviceInfoSensor(SensorEntity):
    """Static device information, most of it useful outside Home Assistant."""

    _attr_has_entity_name = True

    def __init__(
        self,
        device_id: str,
        key: str,
        name: str,
        icon: str,
        value: Any,
        device_info: DeviceInfo,
        extras: dict[str, Any] | None = None,
        coordinator: ZeppCoordinator | None = None,
    ) -> None:
        self._attr_unique_id = f"{device_id}_{key}"
        self._attr_name = name
        self._attr_icon = icon
        self._attr_device_info = device_info
        self._attr_native_value = value
        self._extras = extras or {}
        self._coordinator = coordinator

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self._extras:
            return {}
        attrs = {
            "device_source": self._extras.get("device_source"),
            "auth_key": self._extras.get("auth_key"),
            "bt_mac": self._extras.get("bt_mac"),
            "hardware_version": self._extras.get("hardware_version"),
            "product_id": self._extras.get("product_id"),
            "active": self._extras.get("active_status"),
        }
        if self._coordinator is not None:
            attrs["region_host"] = self._coordinator.client.host
            attrs["user_id"] = self._coordinator.client.userid
        return {key: value for key, value in attrs.items() if value is not None}


class ZeppBatterySensor(CoordinatorEntity[ZeppCoordinator], SensorEntity):
    """Battery level, when the cloud reports one for this model."""

    _attr_has_entity_name = True
    _attr_name = "Battery"
    _attr_icon = "mdi:battery"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: ZeppCoordinator,
        device_id: str,
        device_info: DeviceInfo,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"{device_id}_battery"
        self._attr_device_info = device_info

    @property
    def native_value(self) -> int | None:
        return self.coordinator.data.get("device_batteries", {}).get(self._device_id)


class ZeppSyncStatusSensor(CoordinatorEntity[ZeppCoordinator], SensorEntity):
    """Progress of the historical backfill.

    Kept separate from the data entities so a long backfill is observable
    without guessing from the log.
    """

    _attr_has_entity_name = True
    _attr_name = "History Sync Status"
    _attr_icon = "mdi:cloud-sync-outline"

    def __init__(
        self,
        coordinator: ZeppCoordinator,
        device_id: str,
        device_info: DeviceInfo,
        hass: HomeAssistant,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{device_id}_history_sync_status"
        self._attr_device_info = device_info
        self._hass = hass

    @property
    def native_value(self) -> str:
        state = self.coordinator.backfill
        status = state.get("status", "idle")
        if status == "running":
            return f"Syncing {state.get('days', 0)}/{state.get('total_days', 0)} days"
        return {"done": "Completed", "error": "Error"}.get(status, "Idle")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.coordinator.backfill
        return {
            "status": state.get("status"),
            "progress_percent": state.get("progress", 0),
            "days_imported": state.get("days", 0),
            "days_requested": state.get("total_days", 0),
            "last_error": state.get("last_error"),
            "detail_write_enabled": self.coordinator.detailed_stats,
        }
