"""Polling coordinator for the Zepp integration.

The coordinator owns two clocks:

* the live poll, which keeps the entities current and re-fetches the last few
  days so late uploads are picked up;
* the backfill, which walks history in chunks and writes statistics.

Both feed the same decoder, so a day imported six months ago and a day fetched
five minutes ago are computed identically.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import decoders, history
from .api import (
    ZeppAuthError,
    ZeppClient,
    ZeppConnectionError,
    ZeppRateLimitError,
    async_login,
)
from .const import (
    CONF_APPTOKEN,
    CONF_BACKFILL_DAYS,
    CONF_COUNTRY_CODE,
    CONF_DETAILED_STATS,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_REGION_HOST,
    CONF_SCAN_INTERVAL,
    CONF_USERID,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    LIVE_WINDOW_DAYS,
    SHORT_TERM_DAYS,
)
from .models import DayBundle

_LOGGER = logging.getLogger(__name__)


class ZeppCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetch Zepp data, decode it and keep both statistic resolutions in sync."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=dt.timedelta(minutes=interval),
        )
        self.entry = entry
        self.tz = ZoneInfo(hass.config.time_zone or "UTC")
        self.client = ZeppClient(
            async_get_clientsession(hass),
            entry.data[CONF_APPTOKEN],
            str(entry.data[CONF_USERID]),
            entry.data[CONF_REGION_HOST],
        )
        self._all_devices: list[dict[str, Any]] = list(entry.data.get("devices", []))
        self.devices: list[dict[str, Any]] = list(self._all_devices)
        self.active_sources: set[int] = set()
        self.detailed_stats: bool = entry.options.get(CONF_DETAILED_STATS, True)
        self._statistic_ids: dict[str, str] = {}
        self._sources: dict[str, Any] = {}
        self._last_detail_day: dt.date | None = None
        # Newest 5-minute bucket already written; the next poll rewrites a short
        # tail behind it to absorb Zepp's corrections.
        self._detail_watermark: dt.datetime | None = None
        self._repair_window_days = LIVE_WINDOW_DAYS
        self._lock = asyncio.Lock()
        self.backfill: dict[str, Any] = {
            "status": "idle",
            "progress": 0,
            "days": 0,
            "total_days": 0,
            "last_error": None,
        }

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    @property
    def primary_device_id(self) -> str:
        if self.devices:
            return str(self.devices[0].get("device_id"))
        return str(self.entry.data.get(CONF_USERID))

    def async_resolve_statistic_ids(self) -> dict[str, str]:
        """Map a metric key to the entity_id that carries its statistics."""
        registry = er.async_get(self.hass)
        resolved: dict[str, str] = {}
        for device in self.devices or [{"device_id": self.primary_device_id}]:
            device_id = str(device.get("device_id"))
            for key in history.STAT_DEFINITIONS:
                entity_id = registry.async_get_entity_id(
                    "sensor", DOMAIN, f"{device_id}_{key}"
                )
                if entity_id:
                    resolved.setdefault(key, entity_id)
        self._statistic_ids = resolved
        return resolved

    async def async_wait_for_statistic_ids(self, timeout: float = 30.0) -> dict[str, str]:
        """Wait until the platforms have registered the entities.

        Statistics can only be written once the entity exists, and the
        registry is filled asynchronously by the entity platform. Without this
        the first backfill runs against an empty registry and silently writes
        nothing.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            resolved = self.async_resolve_statistic_ids()
            if len(resolved) >= 3 or asyncio.get_running_loop().time() >= deadline:
                return resolved
            await asyncio.sleep(1.0)

    async def _async_refresh_token(self) -> bool:
        email = self.entry.data.get(CONF_EMAIL)
        password = self.entry.data.get(CONF_PASSWORD)
        if not email or not password:
            return False
        session = async_get_clientsession(self.hass)
        try:
            auth = await async_login(
                session, email, password, self.entry.data.get(CONF_COUNTRY_CODE, "AUTO")
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Zepp re-authentication failed: %s", err)
            return False
        self.client.apptoken = auth["apptoken"]
        new_data = {**self.entry.data, CONF_APPTOKEN: auth["apptoken"]}
        if auth.get("cname"):
            new_data["cname"] = auth["cname"]
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)
        _LOGGER.info("Refreshed the Zepp app token for %s", email)
        return True

    # ------------------------------------------------------------------ #
    # polling
    # ------------------------------------------------------------------ #
    async def _async_update_data(self) -> dict[str, Any]:
        try:
            bundles, live = await self._async_fetch_window()
        except ZeppAuthError as err:
            if await self._async_refresh_token():
                bundles, live = await self._async_fetch_window()
            else:
                raise ConfigEntryAuthFailed("Zepp app token expired") from err
        except ZeppRateLimitError as err:
            raise UpdateFailed(f"Zepp rate-limited the request: {err}") from err
        except ZeppConnectionError as err:
            raise UpdateFailed(str(err)) from err

        if bundles:
            now = dt_util.utcnow()
            since = (
                self._detail_watermark - dt.timedelta(hours=2)
                if self._detail_watermark
                else None
            )
            try:
                await history.async_import(
                    self.hass,
                    bundles,
                    self._statistic_ids or self.async_resolve_statistic_ids(),
                    self.tz,
                    now=now,
                    detailed=self.detailed_stats,
                    detailed_days=SHORT_TERM_DAYS,
                    detail_since=since,
                )
                if self.detailed_stats:
                    self._detail_watermark = now
            except Exception as err:  # noqa: BLE001 - statistics must not break polling
                _LOGGER.warning("Could not import Zepp statistics: %s", err)

        live["last_updated"] = dt_util.utcnow().isoformat()
        live["sources"] = self._sources
        live["devices"] = self.devices
        return live

    async def _async_fetch_window(self) -> tuple[list[DayBundle], dict[str, Any]]:
        """Fetch and decode the live window, plus the sparse event sources."""
        now = dt_util.now(self.tz)
        today = now.date()
        start = today - dt.timedelta(days=self._repair_window_days - 1)
        band_items = await self.client.async_get_band_data(
            start.isoformat(), today.isoformat(), detail=True
        )

        sources = {
            int(item["source"])
            for item in band_items
            if str(item.get("source", "")).isdigit()
        }
        if sources:
            self.active_sources = sources
            # Every watch the account ever paired is still returned by the
            # devices endpoint - one of them has been inactive since 2019.
            # Only the watches that actually feed band_data get entities.
            matching = [
                device
                for device in self._all_devices
                if self._as_int(device.get("device_source")) in sources
            ]
            self.devices = matching or self._all_devices

        bundles: dict[dt.date, DayBundle] = {}
        layouts: dict[str, str] = {}
        for item in band_items:
            activity, sleep, naps = decoders.parse_band_day(item, self.tz)
            if activity is None:
                continue
            bundle = bundles.setdefault(activity.day, DayBundle(day=activity.day))
            bundle.activity = activity
            bundle.sleep = sleep
            if activity.layout:
                layouts[activity.day.isoformat()] = activity.layout

        self._apply_events(bundles, await self._async_collect_events(start))
        await self._async_fetch_daily(bundles, start, today)

        ordered = [bundles[day] for day in sorted(bundles)]
        live = self._compose_live(ordered, layouts)
        return ordered, live

    # Per-source item limits. Minute-level sources return one item per *day*
    # with up to 1440 samples inside, so a large limit is megabytes of JSON for
    # no benefit during a routine poll.
    LIVE_EVENT_LIMITS: dict[str, int] = {
        "stress": 6,
        "spo2": 120,
        "readiness": 6,
        "hrv": 4,
        "respiratory": 4,
        "biocharge": 4,
        "pai": 6,
    }
    HISTORY_EVENT_LIMITS: dict[str, int] = {
        "stress": 400,
        "spo2": 400,
        "readiness": 400,
        "hrv": 30,
        "respiratory": 30,
        "biocharge": 30,
        "pai": 400,
    }

    async def _async_collect_events(
        self, cutoff: dt.date | None, *, history: bool = False
    ) -> dict[dt.date, DayBundle]:
        """Fetch every event source once and bucket it per local day.

        The endpoints return newest-first and ignore range parameters, so a
        single request per source covers months of daily records and the last
        few dozen days of minute-level series. Samples older than ``cutoff``
        are dropped so a poll does not re-import a year of history.
        """
        limits = self.HISTORY_EVENT_LIMITS if history else self.LIVE_EVENT_LIMITS
        sources = (
            ("stress", "all_day_stress", None, False, limits["stress"]),
            ("spo2", "blood_oxygen", None, False, limits["spo2"]),
            ("readiness", "readiness", "watch_score", True, limits["readiness"]),
            ("hrv", "HRVRMSSD", "real_data", True, limits["hrv"]),
            ("respiratory", "RespiratoryRate", "real_data", True, limits["respiratory"]),
            ("biocharge", "Charge", "real_data", True, limits["biocharge"]),
            ("pai", "PaiHealthInfo", None, False, limits["pai"]),
        )
        results = await asyncio.gather(
            *(self._fetch_event_source(name, et, sub, v2, limit) for name, et, sub, v2, limit in sources),
            return_exceptions=True,
        )
        raw: dict[str, list[dict[str, Any]]] = {}
        for (name, *_rest), outcome in zip(sources, results, strict=False):
            if isinstance(outcome, Exception):
                _LOGGER.debug("Zepp source %s failed: %s", name, outcome)
                self._sources[name] = {"ok": False, "error": str(outcome)}
                continue
            raw[name] = outcome
            newest = max((int(i.get("timestamp") or 0) for i in outcome), default=0)
            self._sources[name] = {
                "ok": True,
                "items": len(outcome),
                "newest": (
                    dt.datetime.fromtimestamp(newest / 1000, self.tz).isoformat()
                    if newest
                    else None
                ),
            }

        days: dict[dt.date, DayBundle] = {}

        def bucket(day: dt.date) -> DayBundle | None:
            if cutoff is not None and day < cutoff:
                return None
            return days.setdefault(day, DayBundle(day=day))

        stress_samples, stress_days = decoders.parse_stress(raw.get("stress") or [])
        for sample in stress_samples:
            day = self._local_date(sample.ts)
            if (bundle := bucket(day)) is not None:
                bundle.stress.append(sample)
        for day, summary in stress_days.items():
            if (bundle := bucket(day)) is not None:
                bundle.stress_summary = summary

        for sample in decoders.parse_spo2(raw.get("spo2") or []):
            if (bundle := bucket(self._local_date(sample.ts))) is not None:
                bundle.spo2.append(sample)

        for record in decoders.parse_readiness(raw.get("readiness") or [], self.tz):
            if (bundle := bucket(record["day"])) is not None:
                bundle.readiness = record

        for sample in decoders.parse_samples(
            raw.get("hrv") or [], ("hrv", "rmssd", "value"), offset_key="s"
        ):
            if (bundle := bucket(self._local_date(sample.ts))) is not None:
                bundle.hrv.append(sample)

        for sample in decoders.parse_samples(
            raw.get("respiratory") or [],
            ("measurements",),
            anchor_key="timestamp",
            unit="ms",
            field="measurements",
            base64_field=True,
        ):
            if (bundle := bucket(self._local_date(sample.ts))) is not None:
                bundle.respiratory_rate.append(sample)

        for sample in decoders.parse_biocharge(raw.get("biocharge") or []):
            if (bundle := bucket(self._local_date(sample.ts))) is not None:
                bundle.biocharge.append(sample)

        for day, daily, total in decoders.parse_pai(raw.get("pai") or [], self.tz):
            if (bundle := bucket(day)) is not None:
                bundle.pai = total if total is not None else daily

        return days

    @staticmethod
    def _as_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _local_date(self, timestamp: int) -> dt.date:
        return dt.datetime.fromtimestamp(timestamp, self.tz).date()

    @staticmethod
    def _apply_events(
        bundles: dict[dt.date, DayBundle],
        events: dict[dt.date, DayBundle],
    ) -> None:
        """Merge the per-day event data into the band_data bundles."""
        for day, event_bundle in events.items():
            target = bundles.setdefault(day, DayBundle(day=day))
            if event_bundle.stress:
                target.stress = event_bundle.stress
            if event_bundle.stress_summary:
                target.stress_summary = event_bundle.stress_summary
            if event_bundle.spo2:
                target.spo2 = event_bundle.spo2
            if event_bundle.readiness:
                target.readiness = event_bundle.readiness
            if event_bundle.hrv:
                target.hrv = event_bundle.hrv
            if event_bundle.respiratory_rate:
                target.respiratory_rate = event_bundle.respiratory_rate
            if event_bundle.biocharge:
                target.biocharge = event_bundle.biocharge
            if event_bundle.pai is not None:
                target.pai = event_bundle.pai

    async def _fetch_event_source(
        self,
        name: str,
        event_type: str,
        sub_type: str | None,
        v2: bool,
        limit: int,
    ) -> list[dict[str, Any]]:
        if v2:
            return await self.client.async_get_v2_events(
                event_type, sub_type=sub_type, limit=limit
            )
        return await self.client.async_get_user_events(
            event_type, sub_type=sub_type, limit=limit
        )

    # ``WatchSportStatistics`` only stores days that had a value, so a 2-day
    # window mostly returns nothing. A month of rows is still a tiny payload.
    TRAINING_LOAD_WINDOW_DAYS = 30

    async def _async_fetch_daily(
        self,
        bundles: dict[dt.date, DayBundle],
        start: dt.date,
        today: dt.date,
    ) -> None:
        """Training load, VO2max and the scale, all of which are daily values."""
        start = min(start, today - dt.timedelta(days=self.TRAINING_LOAD_WINDOW_DAYS))
        try:
            payload = await self.client.async_get_watch_statistics(
                "SPORT_LOAD", start.isoformat(), today.isoformat(), limit=60
            )
            for day, values in decoders.parse_watch_statistics(
                payload,
                (
                    "currnetDayTrainLoad",
                    "wtlSum",
                    "wtlSumOptimalMin",
                    "wtlSumOptimalMax",
                ),
            ):
                bundle = bundles.setdefault(day, DayBundle(day=day))
                bundle.training_load = values.get("currnetDayTrainLoad")
                bundle.training_load_7d = values.get("wtlSum")
                bundle.training_load_optimal = (
                    values.get("wtlSumOptimalMin"),
                    values.get("wtlSumOptimalMax"),
                )
            self._sources["training_load"] = {"ok": True, "items": len(payload.get("items") or [])}
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Zepp training load unavailable: %s", err)
            self._sources["training_load"] = {"ok": False, "error": str(err)}

        try:
            payload = await self.client.async_get_watch_statistics(
                "VO2_MAX", start.isoformat(), today.isoformat(), limit=60
            )
            for day, values in decoders.parse_watch_statistics(
                payload, ("vo2max", "vo2Max", "value")
            ):
                bundle = bundles.setdefault(day, DayBundle(day=day))
                bundle.vo2max = next(iter(values.values()), None)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Zepp VO2max unavailable: %s", err)

        try:
            records = await self.client.async_get_weight_records(limit=10)
            for record in decoders.parse_weight({"items": records}):
                if not record.get("ts"):
                    continue
                day = dt_util.as_local(
                    dt.datetime.fromtimestamp(int(record["ts"]) / 1000, tz=dt_util.UTC)
                ).date()
                bundle = bundles.setdefault(day, DayBundle(day=day))
                bundle.weight = record
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Zepp weight records unavailable: %s", err)

    # ------------------------------------------------------------------ #
    # live values
    # ------------------------------------------------------------------ #
    def _compose_live(
        self,
        bundles: list[DayBundle],
        layouts: dict[str, str],
    ) -> dict[str, Any]:
        """Flatten the newest state of every metric into one dict for the sensors."""
        today = dt_util.now(self.tz).date()
        live: dict[str, Any] = {
            "steps": 0,
            "distance": 0,
            "calories": 0,
            "step_goal": None,
            "heart_rate": None,
            "hr_min": None,
            "hr_max": None,
            "hr_avg": None,
            "resting_hr": None,
            "stress": None,
            "stress_min": None,
            "stress_max": None,
            "spo2": None,
            "respiratory_rate": None,
            "readiness": None,
            "breathing": None,
            "hrv": None,
            "pai": None,
            "biocharge": None,
            "skin_temp_delta": None,
            "sleep_score": None,
            "sleep_duration": None,
            "deep_sleep": None,
            "light_sleep": None,
            "rem_sleep": None,
            "awake_time": None,
            "wake_count": None,
            "sleep_rhr": None,
            "sleep_night": None,
            "training_load": None,
            "training_load_7d": None,
            "training_load_min": None,
            "training_load_max": None,
            "vo2max": None,
            "weight": None,
            "bmi": None,
            "body_fat": None,
            "muscle_mass": None,
            "body_water": None,
            "bone_mass": None,
            "layout": None,
        }
        if not bundles:
            return live

        by_day = {bundle.day: bundle for bundle in bundles}
        today_bundle = by_day.get(today)
        if today_bundle and today_bundle.activity:
            activity = today_bundle.activity
            live["layout"] = activity.layout
            if activity.steps is not None:
                present = [v for v in activity.steps.values if v is not None]
                live["steps"] = int(round(sum(present))) if present else 0
                live["distance"] = int(
                    round(
                        sum(v for v in (activity.distance.values if activity.distance else []) if v is not None)
                    )
                    or 0
                )
                live["calories"] = int(
                    round(
                        sum(
                            v
                            for v in (
                                activity.calories_minute.values
                                if activity.calories_minute
                                else []
                            )
                            if v is not None
                        )
                    )
                    or 0
                )
            else:
                # No minute series: the daily total is all we know.
                live["steps"] = activity.steps_total
                live["distance"] = activity.distance_m
                live["calories"] = activity.calories
            live["step_goal"] = activity.step_goal

        # Heart rate: last measured minute of the most recent day that has one.
        for bundle in reversed(bundles):
            series = bundle.activity.heart_rate if bundle.activity else None
            if series is None:
                continue
            valid = [v for v in series.values if v is not None]
            if not valid:
                continue
            live["heart_rate"] = int(valid[-1])
            live["hr_min"] = int(min(valid))
            live["hr_max"] = int(max(valid))
            live["hr_avg"] = round(sum(valid) / len(valid), 1)
            break

        # Sleep: the newest night that actually has duration. Keyed by day, so a
        # night is never carried forward as if it were last night's.
        for bundle in reversed(bundles):
            night = bundle.sleep
            if night is None or not night.duration_min:
                continue
            live["sleep_score"] = night.score
            live["sleep_duration"] = night.duration_min
            live["deep_sleep"] = night.deep_min
            live["light_sleep"] = night.light_min
            live["rem_sleep"] = night.rem_min
            live["awake_time"] = night.awake_min
            live["wake_count"] = night.wake_count
            live["sleep_rhr"] = night.resting_hr
            live["sleep_night"] = night.day.isoformat()
            if night.resting_hr:
                live["resting_hr"] = night.resting_hr
            break

        # Latest sparse sample wins for the "current" reading.
        for key, target in (
            ("stress", "stress"),
            ("spo2", "spo2"),
            ("respiratory_rate", "respiratory_rate"),
            ("biocharge", "biocharge"),
            ("hrv", "hrv"),
        ):
            latest = None
            for bundle in reversed(bundles):
                samples = getattr(bundle, key, None) or []
                if samples:
                    latest = max(samples, key=lambda s: s.ts)
                    break
            if latest is not None:
                live[target] = round(latest.value, 1) if key != "spo2" else int(latest.value)
                live[f"{target}_at"] = dt.datetime.fromtimestamp(
                    latest.ts, tz=dt_util.UTC
                ).isoformat()

        for bundle in reversed(bundles):
            if bundle.stress_summary:
                live["stress_min"] = bundle.stress_summary.get("min")
                live["stress_max"] = bundle.stress_summary.get("max")
                break

        for bundle in reversed(bundles):
            if bundle.readiness:
                live["readiness_at"] = dt.datetime.fromtimestamp(
                    int(bundle.readiness.get("updated") or 0) / 1000, tz=dt_util.UTC
                ).isoformat()
                live["readiness"] = bundle.readiness.get("readiness")
                live["breathing"] = bundle.readiness.get("ahi")
                live["skin_temp_delta"] = bundle.readiness.get("skin_temp_delta")
                hrv_value = bundle.readiness.get("sleep_hrv")
                if hrv_value is not None and live["hrv"] is None:
                    live["hrv"] = hrv_value
                rhr = bundle.readiness.get("sleep_rhr")
                if rhr is not None:
                    live["resting_hr"] = int(rhr)
                break

        if live["breathing"] is None and live["spo2"] is not None:
            # No AHI from the firmware: an ODI of zero desaturations is a
            # perfect breathing quality, otherwise leave it unknown.
            live["breathing"] = 100 if live["spo2"] >= 95 else None

        for bundle in reversed(bundles):
            if bundle.pai is not None:
                live["pai"] = bundle.pai
                live["pai_at"] = bundle.day.isoformat()
                break

        for bundle in reversed(bundles):
            if bundle.training_load is not None or bundle.training_load_7d is not None:
                live["training_load"] = bundle.training_load
                live["training_load_7d"] = bundle.training_load_7d
                live["training_load_min"], live["training_load_max"] = bundle.training_load_optimal
                break

        for bundle in reversed(bundles):
            if bundle.vo2max is not None:
                live["vo2max"] = bundle.vo2max
                break

        for bundle in reversed(bundles):
            if bundle.weight:
                record = bundle.weight
                live["weight"] = record.get("weight")
                live["bmi"] = record.get("bmi")
                live["body_fat"] = record.get("body_fat")
                live["muscle_mass"] = record.get("muscle_mass")
                live["body_water"] = record.get("body_water")
                live["bone_mass"] = record.get("bone_mass")
                break

        # When the phone last uploaded anything for the newest day. This is the
        # number that tells "the cloud is behind" from "the integration is".
        syncs = [
            bundle.activity.synced_at
            for bundle in bundles
            if bundle.activity and bundle.activity.synced_at
        ]
        if syncs:
            live["last_sync"] = dt.datetime.fromtimestamp(
                max(syncs), tz=dt_util.UTC
            ).isoformat()

        battery = {}
        for device in self.devices:
            if device.get("battery") is not None:
                battery[str(device.get("device_id"))] = device["battery"]
        live["device_batteries"] = battery
        return live

    # ------------------------------------------------------------------ #
    # backfill
    # ------------------------------------------------------------------ #
    async def async_backfill(self, days: int = DEFAULT_BACKFILL_DAYS) -> int:
        """Walk history in chunks and import it, oldest first."""
        async with self._lock:
            self.backfill.update(
                status="running", progress=0, days=0, total_days=days, last_error=None
            )
            self.async_update_listeners()

            await self.async_wait_for_statistic_ids()

            today = dt_util.now(self.tz).date()
            start = today - dt.timedelta(days=days)
            cursor = start
            imported = 0
            chunk = 14

            # One request per event source covers the whole range: the
            # endpoints ignore date filters and return newest-first.
            events = await self._async_collect_events(start, history=True)
            _LOGGER.info(
                "Zepp history import: %s days of events collected for %s..%s",
                len(events), start, today,
            )
            while cursor <= today:
                chunk_end = min(cursor + dt.timedelta(days=chunk - 1), today)
                try:
                    items = await self.client.async_get_band_data(
                        cursor.isoformat(), chunk_end.isoformat(), detail=True
                    )
                except ZeppAuthError:
                    self.backfill.update(status="error", last_error="token expired")
                    self.async_update_listeners()
                    raise
                except ZeppConnectionError as err:
                    _LOGGER.warning(
                        "Zepp history chunk %s..%s failed: %s", cursor, chunk_end, err
                    )
                    self.backfill["last_error"] = str(err)
                    cursor = chunk_end + dt.timedelta(days=1)
                    continue

                bundles: dict[dt.date, DayBundle] = {}
                for item in items:
                    activity, sleep, _naps = decoders.parse_band_day(item, self.tz)
                    if activity is None:
                        continue
                    bundle = bundles.setdefault(activity.day, DayBundle(day=activity.day))
                    bundle.activity = activity
                    bundle.sleep = sleep

                if bundles:
                    self._apply_events(bundles, events)
                    await self._async_fetch_daily(bundles, cursor, chunk_end)
                    await history.async_import(
                        self.hass,
                        [bundles[day] for day in sorted(bundles)],
                        self._statistic_ids or self.async_resolve_statistic_ids(),
                        self.tz,
                        now=dt_util.utcnow(),
                        detailed=self.detailed_stats,
                        detailed_days=SHORT_TERM_DAYS,
                    )
                    imported += len(bundles)

                self.backfill.update(
                    progress=min(99, int((imported / max(1, days)) * 100)),
                    days=imported,
                )
                self.async_update_listeners()
                cursor = chunk_end + dt.timedelta(days=1)
                await asyncio.sleep(0.5)

            self.backfill.update(status="done", progress=100, days=imported)
            self.async_update_listeners()
            return imported
