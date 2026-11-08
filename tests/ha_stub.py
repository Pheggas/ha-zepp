"""A very small Home Assistant stand-in, enough to import the integration.

The integration runs inside Home Assistant, which is not installed here. Stubbing
the modules it imports is the cheapest way to catch the class of bug that only
shows up at import time: a renamed constant, a wrong base class, an attribute
that does not exist on the object being subclassed.

This is a test harness, not a Home Assistant emulator. Nothing here pretends to
implement behaviour the integration relies on at runtime.
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Bratislava")


def _module(name: str, package: bool = False) -> types.ModuleType:
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    mod = types.ModuleType(name)
    if package:
        mod.__path__ = []  # type: ignore[attr-defined]
    sys.modules[name] = mod
    return mod


def install(tz: ZoneInfo = TZ) -> None:
    """Register the stub modules in ``sys.modules``."""
    ha = _module("homeassistant", package=True)

    # --- core ----------------------------------------------------------- #
    core = _module("homeassistant.core")
    core.HomeAssistant = object
    core.ServiceCall = object
    core.callback = lambda fn: fn
    core.valid_entity_id = lambda value: True

    const = _module("homeassistant.const")
    const.Platform = types.SimpleNamespace(SENSOR="sensor", BUTTON="button")
    const.PERCENTAGE = "%"
    const.EntityStateAttribute = types.SimpleNamespace(UNIT_OF_MEASUREMENT="unit_of_measurement")
    const.UnitOfLength = types.SimpleNamespace(METERS="m")
    const.UnitOfTime = types.SimpleNamespace(MINUTES="min")
    const.UnitOfEnergy = types.SimpleNamespace(KILO_CALORIE="kcal")
    const.UnitOfMass = types.SimpleNamespace(KILOGRAMS="kg")

    exceptions = _module("homeassistant.exceptions")
    exceptions.HomeAssistantError = type("HomeAssistantError", (Exception,), {})
    exceptions.ConfigEntryAuthFailed = type("ConfigEntryAuthFailed", (Exception,), {})
    exceptions.ConfigEntryNotReady = type("ConfigEntryNotReady", (Exception,), {})

    data_entry_flow = _module("homeassistant.data_entry_flow")
    data_entry_flow.FlowResult = dict
    data_entry_flow.AbortFlow = type("AbortFlow", (Exception,), {"reason": ""})

    # --- config entries -------------------------------------------------- #
    config_entries = _module("homeassistant.config_entries")
    config_entries.ConfigEntry = type("ConfigEntry", (), {})
    config_entries.ConfigFlow = type(
        "ConfigFlow",
        (),
        {
            "VERSION": 1,
            "__init_subclass__": classmethod(lambda cls, **kwargs: None),
        },
    )
    config_entries.OptionsFlow = type("OptionsFlow", (), {})
    config_entries.HANDLERS = type("Handlers", (), {"register": lambda self, domain: (lambda cls: cls)})()
    config_entries.callback = lambda fn: fn

    # --- util ------------------------------------------------------------ #
    util = _module("homeassistant.util", package=True)
    dt_util = _module("homeassistant.util.dt")
    dt_util.UTC = timezone.utc
    dt_util.as_utc = lambda moment: moment.astimezone(timezone.utc)
    dt_util.as_local = lambda moment: moment.astimezone(tz)
    dt_util.utcnow = lambda: datetime.now(timezone.utc)
    dt_util.now = lambda zone=None: datetime.now(zone or tz)
    dt_util.parse_datetime = lambda text: datetime.fromisoformat(text)
    util.dt = dt_util

    util_async = _module("homeassistant.util.async_")
    util_async.run_callback_threadsafe = lambda hass, fn, *a, **k: fn(*a, **k)

    # --- helpers --------------------------------------------------------- #
    helpers = _module("homeassistant.helpers", package=True)

    helper_aiohttp = _module("homeassistant.helpers.aiohttp_client")
    helper_aiohttp.async_get_clientsession = lambda hass: None

    helper_cv = _module("homeassistant.helpers.config_validation")
    helper_cv.config_entry_only_config_schema = lambda domain: {}

    helper_entity = _module("homeassistant.helpers.entity")
    helper_entity.EntityCategory = types.SimpleNamespace(DIAGNOSTIC="diagnostic")

    helper_platform = _module("homeassistant.helpers.entity_platform")
    helper_platform.AddEntitiesCallback = object

    helper_registry = _module("homeassistant.helpers.entity_registry")
    helper_registry.async_get = lambda hass: None

    helper_device = _module("homeassistant.helpers.device_registry")
    helper_device.DeviceInfo = dict
    helper_device.CONNECTION_NETWORK_MAC = "mac"
    helper_device.async_get = lambda hass: None

    helper_dispatcher = _module("homeassistant.helpers.dispatcher")
    helper_dispatcher.async_dispatcher_connect = lambda hass, signal, target: (lambda: None)
    helper_dispatcher.async_dispatcher_send = lambda hass, signal, *args: None

    helper_storage = _module("homeassistant.helpers.storage")
    helper_storage.Store = type("Store", (), {})

    helper_update = _module("homeassistant.helpers.update_coordinator")

    class DataUpdateCoordinator:
        # Home Assistant's coordinator is Generic; subscripts must keep working.
        __class_getitem__ = classmethod(lambda cls, item: cls)

        def __init__(self, hass, logger, *, name=None, update_interval=None):
            self.hass = hass
            self.logger = logger
            self.name = name
            self.update_interval = update_interval
            self.data: dict[str, Any] | None = None
            self.last_update_success = True

        def async_update_listeners(self) -> None:
            pass

        async def async_request_refresh(self) -> None:
            pass

        async def async_config_entry_first_refresh(self) -> None:
            pass

    class CoordinatorEntity:
        __class_getitem__ = classmethod(lambda cls, item: cls)

        def __init__(self, coordinator):
            self.coordinator = coordinator

        @property
        def available(self) -> bool:
            return True

        async def async_added_to_hass(self) -> None:
            pass

        def async_write_ha_state(self) -> None:
            pass

        def async_on_remove(self, fn) -> None:
            pass

    helper_update.DataUpdateCoordinator = DataUpdateCoordinator
    helper_update.CoordinatorEntity = CoordinatorEntity
    helper_update.UpdateFailed = type("UpdateFailed", (Exception,), {})

    helper_selector = _module("homeassistant.helpers.selector")
    helper_selector.SelectSelector = lambda config: config
    helper_selector.SelectSelectorConfig = lambda **kwargs: kwargs
    helper_selector.SelectSelectorMode = types.SimpleNamespace(DROPDOWN="dropdown", LIST="list")
    helper_selector.SelectOptionDict = lambda **kwargs: kwargs
    helper_selector.NumberSelector = lambda config: config
    helper_selector.NumberSelectorConfig = lambda **kwargs: kwargs
    helper_selector.NumberSelectorMode = types.SimpleNamespace(BOX="box")
    helper_selector.BooleanSelector = lambda: True
    helper_selector.EntitySelector = lambda config: config
    helper_selector.EntitySelectorConfig = lambda **kwargs: kwargs

    # --- components ------------------------------------------------------ #
    components = _module("homeassistant.components", package=True)

    sensor = _module("homeassistant.components.sensor")
    sensor.SensorEntity = type("SensorEntity", (), {})
    sensor.SensorDeviceClass = types.SimpleNamespace(
        BATTERY="battery", DISTANCE="distance", DURATION="duration", WEIGHT="weight"
    )
    sensor.SensorStateClass = types.SimpleNamespace(
        MEASUREMENT="measurement", TOTAL_INCREASING="total_increasing", TOTAL="total"
    )

    button = _module("homeassistant.components.button")
    button.ButtonEntity = type("ButtonEntity", (), {})
    button.ButtonDeviceClass = types.SimpleNamespace(UPDATE="update")

    diagnostics = _module("homeassistant.components.diagnostics")
    diagnostics.async_redact_data = lambda data, keys: data

    recorder = _module("homeassistant.components.recorder", package=True)
    recorder_models = _module("homeassistant.components.recorder.models")

    class StatisticData(dict):
        pass

    class StatisticMetaData(dict):
        pass

    recorder_models.StatisticData = StatisticData
    recorder_models.StatisticMetaData = StatisticMetaData
    recorder_models.StatisticMeanType = types.SimpleNamespace(ARITHMETIC="arithmetic", NONE="none")
    recorder_models.StatisticsShortTerm = object
    recorder_models.StatisticsMeta = object

    recorder_statistics = _module("homeassistant.components.recorder.statistics")
    recorder_statistics.async_import_statistics = lambda *a, **k: None

    recorder_const = _module("homeassistant.components.recorder.const")
    recorder_const.DOMAIN = "recorder"

    recorder.get_instance = lambda hass: None
    recorder.models = recorder_models
    recorder.statistics = recorder_statistics

    # --- third party ----------------------------------------------------- #
    _module("probatio")
    vol = _module("voluptuous")
    vol.Schema = lambda schema=None, **kwargs: schema
    vol.Required = lambda key, default=None, **kwargs: key
    vol.Optional = lambda key, default=None, **kwargs: key
    vol.In = lambda values: values
    vol.All = lambda *validators: validators
    if "sqlalchemy" not in sys.modules:
        try:  # pragma: no cover
            import sqlalchemy  # noqa: F401
        except ImportError:
            sa = _module("sqlalchemy", package=True)
            sa.select = lambda *a, **k: None
            sa.func = types.SimpleNamespace(sum=lambda *a: None, max=lambda *a: None, min=lambda *a: None, avg=lambda *a: None)
            sa.text = lambda *a, **k: None
            sa.and_ = lambda *a, **k: None
            _module("sqlalchemy.orm", package=True)
            _module("sqlalchemy.orm.session").Session = object
