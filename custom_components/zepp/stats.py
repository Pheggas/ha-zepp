"""Writing decoded Zepp data into Home Assistant's recorder.

Two different tables matter, and they are filled in two different ways:

``statistics`` (hourly, kept forever)
    Written through the supported ``async_import_statistics`` API. That API
    rejects anything that is not aligned to the top of the hour, which is why
    the minute-level data has to be aggregated before it gets here.

``statistics_short_term`` (5 minutes, kept for ``purge_keep_days``)
    This is the table the history graph reads for short ranges, and there is no
    public import API for it - Home Assistant only fills it from live state
    changes. Writing it directly is the only way to make *past* days show
    minute-level detail instead of one or two daily bumps, so it is done here
    behind an explicit option. Every row is computed from the same minute data
    as the hourly rows, so the two resolutions agree.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Iterable

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.const import DOMAIN as RECORDER_DOMAIN
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

# Rows per INSERT batch, to keep a single statement well away from parameter
# limits on SQLite.
_BATCH = 500


def _mean_type(has_mean: bool) -> Any:
    """Return the mean-type enum, tolerating both enum layouts."""
    try:
        from homeassistant.components.recorder.models import StatisticMeanType  # noqa: PLC0415
    except ImportError:  # pragma: no cover - older/newer layout
        return 1 if has_mean else 0
    return StatisticMeanType.ARITHMETIC if has_mean else StatisticMeanType.NONE


def build_metadata(statistic_id: str, unit: str | None, has_sum: bool) -> StatisticMetaData:
    """Build the metadata block required by the import API."""
    has_mean = not has_sum
    return StatisticMetaData(
        has_mean=has_mean,
        mean_type=_mean_type(has_mean),
        has_sum=has_sum,
        name=None,
        source=RECORDER_DOMAIN,
        statistic_id=statistic_id,
        unit_of_measurement=unit,
        unit_class=None,
    )


async def async_import_hourly(
    hass: HomeAssistant,
    statistic_id: str,
    unit: str | None,
    has_sum: bool,
    rows: Iterable[StatisticData],
) -> int:
    """Import hour-aligned rows into long-term statistics."""
    rows = list(rows)
    if not rows:
        return 0
    metadata = build_metadata(statistic_id, unit, has_sum)
    try:
        async_import_statistics(hass, metadata, rows)
    except Exception as err:  # noqa: BLE001 - never break polling on a stats write
        _LOGGER.warning("Could not import long-term statistics for %s: %s", statistic_id, err)
        return 0
    return len(rows)


# --------------------------------------------------------------------------- #
# 5-minute rows
# --------------------------------------------------------------------------- #
def _short_term_models():
    """Import the recorder models from whichever module exposes them."""
    for module_name in (
        "homeassistant.components.recorder.db_schema",
        "homeassistant.components.recorder.models",
    ):
        try:
            module = __import__(module_name, fromlist=["StatisticsShortTerm", "StatisticsMeta"])
            return module.StatisticsShortTerm, module.StatisticsMeta
        except (ImportError, AttributeError):
            continue
    return None, None


def _write_short_term(
    hass: HomeAssistant,
    statistic_id: str,
    rows: list[dict[str, float | int | None]],
) -> int:
    """Upsert 5-minute rows for one statistic. Runs in the executor."""
    model, meta_model = _short_term_models()
    if model is None or meta_model is None:
        _LOGGER.debug("Short-term statistics model not importable; skipping detail write")
        return 0

    from homeassistant.components.recorder.util import session_scope  # noqa: PLC0415
    from sqlalchemy import select  # noqa: PLC0415

    try:
        with session_scope(hass=hass) as session:
            meta_row = session.execute(
                select(meta_model.id).where(meta_model.statistic_id == statistic_id)
            ).fetchone()
            if meta_row is None:
                # The entity has not produced a statistic yet; the next poll
                # (once the entity has state) will find it.
                return 0
            metadata_id = int(meta_row[0])

            payload = []
            for row in rows:
                payload.append(
                    {
                        "metadata_id": metadata_id,
                        "start_ts": int(row["start"].timestamp()),
                        "state": row.get("state"),
                        "sum": row.get("sum"),
                        "mean": row.get("mean"),
                        "min": row.get("min"),
                        "max": row.get("max"),
                        "last_reset_ts": None,
                    }
                )

            if not payload:
                return 0

            # Upsert rather than select-then-insert: two writers (the poll and
            # a backfill) can race on the same bucket, and the unique index on
            # (metadata_id, start_ts) turns that into a constraint error.
            dialect = session.bind.dialect.name if session.bind else "sqlite"
            if dialect == "postgresql":  # pragma: no cover
                from sqlalchemy.dialects.postgresql import insert  # noqa: PLC0415
            else:
                from sqlalchemy.dialects.sqlite import insert  # noqa: PLC0415

            for start in range(0, len(payload), _BATCH):
                batch = payload[start : start + _BATCH]
                stmt = insert(model).values(batch)
                stmt = stmt.on_conflict_do_update(
                    index_elements=[model.metadata_id, model.start_ts],
                    set_={
                        "state": stmt.excluded.state,
                        "sum": stmt.excluded.sum,
                        "mean": stmt.excluded.mean,
                        "min": stmt.excluded.min,
                        "max": stmt.excluded.max,
                    },
                )
                session.execute(stmt)
            session.commit()
            return len(payload)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Could not write 5-minute statistics for %s: %s", statistic_id, err)
        return 0


async def async_import_short_term(
    hass: HomeAssistant,
    statistic_id: str,
    rows: list[dict[str, float | int | None]],
) -> int:
    """Write 5-minute rows, in the executor, swallowing recoverable errors."""
    if not rows:
        return 0
    # Database work belongs on the recorder's executor; the generic one is
    # flagged as misuse and is slower for this.
    return await get_instance(hass).async_add_executor_job(
        _write_short_term, hass, statistic_id, rows
    )


def _write_states_probe(*_args: Any) -> int:
    """Placeholder kept out of the way; state writes are intentionally absent."""
    return 0


# --------------------------------------------------------------------------- #
# helpers shared with the coordinator
# --------------------------------------------------------------------------- #
def hour_start(moment: datetime) -> datetime:
    """Top of the hour, in UTC, which is what the import API insists on."""
    local = dt_util.as_utc(moment)
    return local.replace(minute=0, second=0, microsecond=0)


def five_minute_start(moment: datetime) -> datetime:
    """Round a timestamp down to the 5-minute grid."""
    moment = dt_util.as_utc(moment)
    return moment.replace(minute=moment.minute - moment.minute % 5, second=0, microsecond=0)


def iter_buckets(start: datetime, end: datetime, minutes: int) -> Iterable[datetime]:
    """Yield bucket starts of ``minutes`` length covering ``[start, end)``."""
    cursor = start
    while cursor < end:
        yield cursor
        cursor += timedelta(minutes=minutes)
