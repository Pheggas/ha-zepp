"""Turn decoded days into Home Assistant statistics rows.

The whole point of this module is resolution. The watch records per minute, the
phone uploads in batches, and the cloud keeps the per-minute detail forever.
Home Assistant can store that detail at two resolutions, so both are produced
from the same decoded minutes:

* one row per local hour, written through the supported import API and kept
  forever;
* one row per five minutes for the recent window, which is what the history
  graph reads when you zoom into a day.

Nothing here invents data. When a series cannot be decoded, the day is imported
as the daily total at the last hour it covers, and that is recorded in the
diagnostics rather than smoothed into a fake curve.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.components.recorder.models import StatisticData
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from . import stats
from .models import DayBundle, MinuteSeries
from .entities import STAT_DEFINITIONS
from .stats import hour_start

_LOGGER = logging.getLogger(__name__)


def nightly_date(bundle: DayBundle) -> date | None:
    """The morning a night belongs to, as a local date."""
    if bundle.sleep is None or not bundle.sleep.end_ts:
        return None
    return dt_util.as_local(
        datetime.fromtimestamp(bundle.sleep.end_ts, tz=dt_util.UTC)
    ).date()


def _day_start(day: date, tz: ZoneInfo) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=tz)


def _series_slice(series: MinuteSeries | None, start: int, minutes: int) -> list[float | None]:
    if series is None:
        return []
    return series.values[start : start + minutes]


def _metrics_for_bucket(
    series: MinuteSeries | None,
    start: int,
    minutes: int,
) -> tuple[float | None, float | None, float | None]:
    """(mean, min, max) for one bucket of a measurement series."""
    chunk = [v for v in _series_slice(series, start, minutes) if v is not None]
    if not chunk:
        return None, None, None
    return sum(chunk) / len(chunk), min(chunk), max(chunk)


def _proportional(series: MinuteSeries | None, total: float | None) -> list[float | None]:
    """Scale a per-minute series so it sums to a known daily total."""
    if series is None or total is None:
        return []
    current = sum(v for v in series.values if v is not None)
    if current <= 0:
        return [None] * len(series.values)
    factor = total / current
    return [None if v is None else v * factor for v in series.values]


def _sample_buckets(
    samples: list[Any],
    minutes: int,
) -> dict[datetime, tuple[float, float, float]]:
    """Group sparse samples into time buckets."""
    result: dict[datetime, tuple[float, float, float]] = {}
    if not samples:
        return result
    grouped: dict[datetime, list[float]] = {}
    for sample in samples:
        moment = dt_util.as_local(datetime.fromtimestamp(sample.ts, tz=dt_util.UTC))
        bucket = moment.replace(
            minute=moment.minute - moment.minute % minutes,
            second=0,
            microsecond=0,
        )
        bucket = dt_util.as_utc(bucket)
        grouped.setdefault(bucket, []).append(float(sample.value))
    for bucket, values in grouped.items():
        result[bucket] = (sum(values) / len(values), min(values), max(values))
    return result


def build_rows(
    bundles: list[DayBundle],
    tz: ZoneInfo,
    *,
    minutes: int,
    now: datetime,
    base_sums: dict[str, float] | None = None,
    cumulative_keys: tuple[str, ...] = ("steps", "distance", "calories"),
) -> dict[str, list[dict[str, Any]]]:
    """Build rows for every key at the given bucket size.

    ``minutes`` is 60 for long-term statistics or 5 for short-term ones. The
    returned dicts carry ``start`` as an aware UTC datetime plus the values the
    caller needs; converting them to the recorder's ``StatisticData`` (which
    insists on top-of-hour timestamps) happens in the hourly importer.
    """
    base_sums = dict(base_sums or {})
    rows: dict[str, list[dict[str, Any]]] = {}
    running = dict(base_sums)
    per_bucket_minutes = minutes
    cut_off = dt_util.as_utc(now)

    for bundle in sorted(bundles, key=lambda b: b.day):
        day = bundle.day
        day_start = _day_start(day, tz)
        activity = bundle.activity

        # Hour/bucket starts for this local day.
        buckets: list[datetime] = []
        cursor = day_start
        while cursor < day_start + timedelta(days=1):
            buckets.append(dt_util.as_utc(cursor))
            cursor += timedelta(minutes=per_bucket_minutes)

        steps_series = activity.steps if activity else None
        distance_series = activity.distance if activity else None
        calories_series = activity.calories_minute if activity else None
        if distance_series is None and steps_series is not None and activity:
            scaled = _proportional(steps_series, float(activity.distance_m) or None)
            distance_series = MinuteSeries(day, scaled) if scaled else None
        if calories_series is None and steps_series is not None and activity:
            scaled = _proportional(steps_series, float(activity.calories) or None)
            calories_series = MinuteSeries(day, scaled) if scaled else None

        hours_of_day = 24 * 60 // per_bucket_minutes
        minutes_per_bucket = per_bucket_minutes

        # ---- cumulative activity -------------------------------------- #
        for key, series, total in (
            ("steps", steps_series, activity.steps_total if activity else 0),
            ("distance", distance_series, activity.distance_m if activity else 0),
            ("calories", calories_series, activity.calories if activity else 0),
        ):
            if key not in cumulative_keys:
                continue
            if series is None:
                if not total:
                    continue
                # Nothing to distribute: place the whole day at its last
                # bucket rather than inventing an hourly curve.
                last_bucket = buckets[-1]
                if last_bucket >= cut_off:
                    last_bucket = hour_start(cut_off) - timedelta(minutes=minutes)
                    if last_bucket < buckets[0]:
                        continue
                running[key] = running.get(key, 0.0) + float(total)
                rows.setdefault(key, []).append(
                    {
                        "start": last_bucket,
                        "state": float(total),
                        "sum": running[key],
                        "estimated": True,
                    }
                )
                continue

            day_accum = 0.0
            for index, bucket_start in enumerate(buckets):
                if bucket_start >= cut_off:
                    break
                chunk = _series_slice(series, index * minutes_per_bucket, minutes_per_bucket)
                present = [v for v in chunk if v is not None]
                if not present:
                    continue
                bucket_value = float(sum(present))
                day_accum += bucket_value
                running[key] = running.get(key, 0.0) + bucket_value
                rows.setdefault(key, []).append(
                    {
                        "start": bucket_start,
                        "state": round(day_accum, 3),
                        "sum": round(running[key], 3),
                    }
                )
            # A day whose decoded minutes fall short of the reported total
            # leaves the running sum behind the truth; correct it at the last
            # bucket so the long-term total stays honest.
            if total and abs(day_accum - float(total)) > 1:
                delta = float(total) - day_accum
                last = rows.get(key)
                if last and last[-1]["start"] >= buckets[0]:
                    last[-1]["sum"] = round(last[-1]["sum"] + delta, 3)
                    running[key] = last[-1]["sum"]

        # ---- measurement series --------------------------------------- #
        hr = activity.heart_rate if activity else None
        if per_bucket_minutes >= 60:
            for index, bucket_start in enumerate(buckets):
                if bucket_start >= cut_off:
                    break
                mean, low, high = _metrics_for_bucket(hr, index * 60, 60)
                if mean is None:
                    continue
                rows.setdefault("heart_rate", []).append(
                    {"start": bucket_start, "mean": mean, "min": low, "max": high, "state": mean}
                )

        # ---- sparse series -------------------------------------------- #
        for key, samples in (
            ("stress", bundle.stress),
            ("spo2", bundle.spo2),
            ("respiratory_rate", bundle.respiratory_rate),
            ("biocharge", bundle.biocharge),
            ("hrv", bundle.hrv),
        ):
            grouped = _sample_buckets(samples, per_bucket_minutes)
            for bucket_start in sorted(grouped):
                if bucket_start >= cut_off:
                    continue
                mean, low, high = grouped[bucket_start]
                rows.setdefault(key, []).append(
                    {"start": bucket_start, "mean": mean, "min": low, "max": high, "state": mean}
                )

        # ---- one value per day ---------------------------------------- #
        daily: dict[str, float | None] = {
            "readiness_score": bundle.readiness.get("readiness"),
            "breathing_score": bundle.readiness.get("ahi"),
            "pai": bundle.pai,
            "training_load_today": bundle.training_load,
            "training_load": bundle.training_load_7d,
            "vo2max": bundle.vo2max,
        }
        anchor = dt_util.as_utc(day_start)
        if bundle.sleep is not None and bundle.sleep.duration_min:
            nightly = nightly_date(bundle)
            if nightly is not None:
                anchor = dt_util.as_utc(_day_start(nightly, tz))
            daily.update(
                {
                    "sleep_score": bundle.sleep.score,
                    "sleep_duration": bundle.sleep.duration_min,
                    "deep_sleep": bundle.sleep.deep_min,
                    "light_sleep": bundle.sleep.light_min,
                    "rem_sleep": bundle.sleep.rem_min,
                    "awake_time": bundle.sleep.awake_min,
                    "wake_count": bundle.sleep.wake_count,
                    "sleep_rhr": bundle.sleep.resting_hr,
                }
            )
        for key, value in daily.items():
            if value is None:
                continue
            value = float(value)
            if anchor >= cut_off:
                continue
            rows.setdefault(key, []).append(
                {"start": anchor, "mean": value, "min": value, "max": value, "state": value}
            )

    # Drop buckets that land on a non-hour boundary when the caller asked for
    # hourly rows; they cannot be imported.
    if per_bucket_minutes == 60:
        for key, key_rows in rows.items():
            rows[key] = [row for row in key_rows if row["start"].minute == 0]

    return rows


def to_statistic_data(rows: list[dict[str, Any]]) -> list[StatisticData]:
    """Convert built rows into the recorder's StatisticData shape."""
    output: list[StatisticData] = []
    for row in rows:
        start = row["start"]
        if start.minute != 0 or start.second != 0:
            continue
        entry: dict[str, Any] = {"start": start}
        for field in ("state", "sum", "mean", "min", "max"):
            if row.get(field) is not None:
                entry[field] = float(row[field])
        output.append(entry)  # type: ignore[typeddict-item]
    return output


async def async_get_base_sums(
    hass: HomeAssistant,
    statistic_ids: dict[str, str],
    before: datetime,
) -> dict[str, float]:
    """Read the running sum in effect just before ``before`` for each key.

    Reseeding the running total from the database is what keeps re-imports
    idempotent: the same day always produces the same absolute ``sum`` no matter
    how many times it is written.
    """
    from homeassistant.components.recorder import get_instance  # noqa: PLC0415

    return await get_instance(hass).async_add_executor_job(
        _base_sums_sync, hass, statistic_ids, before
    )


def _base_sums_sync(
    hass: HomeAssistant,
    statistic_ids: dict[str, str],
    before: datetime,
) -> dict[str, float]:
    from sqlalchemy import func, select  # noqa: PLC0415

    from homeassistant.components.recorder.util import session_scope  # noqa: PLC0415

    model, meta_model = stats._short_term_models()  # noqa: SLF001
    if model is None or meta_model is None:
        _LOGGER.warning("Zepp cannot read recorder models; running sums will restart at 0")
        return {}

    statistics_model = None
    for module_name in (
        "homeassistant.components.recorder.db_schema",
        "homeassistant.components.recorder.models",
    ):
        try:
            module = __import__(module_name, fromlist=["Statistics"])
            statistics_model = module.Statistics
            break
        except (ImportError, AttributeError):
            continue
    if statistics_model is None:
        # Silently returning {} here restarts every cumulative series at zero,
        # which looks like a huge step in the history. Say so instead.
        _LOGGER.warning("Zepp could not import the statistics model; sums restart at 0")
        return {}

    cutoff = before.timestamp()
    result: dict[str, float] = {}
    with session_scope(hass=hass, read_only=True) as session:
        for key, statistic_id in statistic_ids.items():
            row = session.execute(
                select(statistics_model.sum)
                .join(meta_model, meta_model.id == statistics_model.metadata_id)
                .where(
                    meta_model.statistic_id == statistic_id,
                    statistics_model.start_ts < cutoff,
                )
                .order_by(statistics_model.start_ts.desc())
                .limit(1)
            ).fetchone()
            if row and row[0] is not None:
                result[key] = float(row[0])
    _LOGGER.debug("Zepp running-sum seeds before %s: %s", before, result)
    return result


async def async_import(
    hass: HomeAssistant,
    bundles: list[DayBundle],
    statistic_ids: dict[str, str],
    tz: ZoneInfo,
    *,
    now: datetime,
    detailed: bool,
    detailed_days: int,
    detail_since: datetime | None = None,
) -> dict[str, int]:
    """Import a batch of days at both resolutions."""
    if not bundles:
        return {"hourly": 0, "detail": 0}

    oldest = min(bundle.day for bundle in bundles)
    window_start = _day_start(oldest, tz)
    base = await async_get_base_sums(hass, statistic_ids, window_start)

    hourly_rows = build_rows(bundles, tz, minutes=60, now=now, base_sums=base)
    hourly_count = 0
    for key, key_rows in hourly_rows.items():
        statistic_id = statistic_ids.get(key)
        if not statistic_id or not key_rows:
            continue
        unit, has_sum = STAT_DEFINITIONS.get(key, (None, False))
        hourly_count += await stats.async_import_hourly(
            hass, statistic_id, unit, has_sum, to_statistic_data(key_rows)
        )

    detail_count = 0
    if detailed:
        detail_cut = dt_util.as_utc(now) - timedelta(days=detailed_days)
        recent = [b for b in bundles if _day_start(b.day, tz) >= detail_cut]
        if recent:
            detail_base = base if recent[0] is bundles[0] else await async_get_base_sums(
                hass, statistic_ids, _day_start(recent[0].day, tz)
            )
            detail_rows = build_rows(recent, tz, minutes=5, now=now, base_sums=detail_base)
            for key, key_rows in detail_rows.items():
                statistic_id = statistic_ids.get(key)
                if not statistic_id or not key_rows:
                    continue
                if detail_since is not None:
                    # Only the tail of the curve moves between polls; rewriting
                    # ten days of 5-minute rows every five minutes would be
                    # thousands of statements for data that cannot have changed.
                    key_rows = [row for row in key_rows if row["start"] >= detail_since]
                    if not key_rows:
                        continue
                detail_count += await stats.async_import_short_term(hass, statistic_id, key_rows)

    return {"hourly": hourly_count, "detail": detail_count}
