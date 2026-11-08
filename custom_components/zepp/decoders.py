"""Pure decoders for the undocumented Zepp cloud payloads.

Every function here is side-effect free and takes plain bytes/dicts, so the
whole layer can be exercised from the probe script and from unit tests without
Home Assistant.

Two payload shapes carry the high-resolution data:

``data_hr``
    base64 of one byte per minute of the local day. ``0`` and ``254`` mean "not
    measured", valid readings are 30-220 bpm.

``data``
    base64 of a fixed-width record per minute. Which byte of the record holds
    the step count is not documented and has been reported differently by
    different reverse-engineering efforts, so this module *derives* it: the
    daily totals in ``summary.stp`` are known, and the channel whose daily sum
    matches ``stp.ttl`` is the step channel. That makes the decoder
    self-calibrating instead of hard-coding an offset that may differ between
    watch models or firmware versions.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .models import ActivityDay, MinuteSeries, Sample, SleepNight

_LOGGER = logging.getLogger(__name__)

HR_SENTINELS = frozenset({0, 254, 255})
HR_VALID_MIN = 25
HR_VALID_MAX = 240

SLEEP_MODE_AWAKE = 7
SLEEP_MODE_DEEP = 5
SLEEP_MODE_LIGHT = 4
SLEEP_MODE_REM = 8

# Tolerances for channel identification in the activity blob.
_STEPS_TOLERANCE = 0.02
_TOTAL_TOLERANCE = 0.08


# --------------------------------------------------------------------------- #
# base64 helpers
# --------------------------------------------------------------------------- #
def b64_bytes(raw: str | bytes | None) -> bytes:
    """Decode a base64 field, tolerating whitespace and missing padding."""
    if not raw:
        return b""
    if isinstance(raw, bytes):
        raw = raw.decode("ascii", "ignore")
    padded = raw.strip()
    padding = (-len(padded)) % 4
    if padding:
        padded += "=" * padding
    try:
        return base64.b64decode(padded)
    except (binascii.Error, ValueError):
        _LOGGER.debug("Could not base64-decode a %d character field", len(padded))
        return b""


def b64_json(raw: str | bytes | None) -> dict[str, Any]:
    """Decode the base64-wrapped JSON used by ``band_data.summary``."""
    payload = b64_bytes(raw)
    if not payload:
        return {}
    try:
        decoded = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        _LOGGER.debug("summary field was not base64 JSON (%d bytes)", len(payload))
        return {}
    return decoded if isinstance(decoded, dict) else {}


def as_float(value: Any) -> float | None:
    """Numbers sometimes arrive as strings, and ``-1`` is a sentinel."""
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number in (-1.0, -20000.0, -274.0, -361.0):
        return None
    return number


# --------------------------------------------------------------------------- #
# time helpers
# --------------------------------------------------------------------------- #
def local_midnight(day: date, tz: ZoneInfo) -> datetime:
    """Local midnight of ``day`` (DST-correct, never a fixed offset)."""
    return datetime(day.year, day.month, day.day, tzinfo=tz)


def minute_timestamp(day: date, minute: int, tz: ZoneInfo) -> int:
    """Unix seconds for minute index ``minute`` of the local day ``day``.

    The payloads carry a fixed number of slots per day, so index *i* means
    "wall clock 00:00 plus *i* minutes" and the arithmetic is deliberately
    wall-clock rather than elapsed-time. That keeps a 1440-slot array aligned
    on the one DST day a year where the two differ; the cost of getting it the
    other way round would be a one-hour shift on that single day.
    """
    return int((local_midnight(day, tz) + timedelta(minutes=minute)).timestamp())


# --------------------------------------------------------------------------- #
# activity blob (steps / distance / calories per minute)
# --------------------------------------------------------------------------- #
def _channels(raw: bytes, stride: int) -> list[list[int]]:
    """Split a fixed-width record blob into one list per byte position."""
    usable = len(raw) - (len(raw) % stride)
    return [
        [raw[i] for i in range(offset, usable, stride)] for offset in range(stride)
    ]


def _best_match(
    series: list[float],
    target: float | None,
    tolerance: float,
) -> float | None:
    """Relative error between a candidate channel sum and a known daily total."""
    if target is None or target <= 0:
        return None
    total = sum(series)
    if total <= 0:
        return None
    error = abs(total - target) / target
    return error if error <= tolerance else None


def decode_activity(
    raw: bytes,
    day: date,
    total_steps: int,
    total_distance: int,
    total_calories: int,
    width_hint: int | None = None,
) -> tuple[MinuteSeries | None, MinuteSeries | None, MinuteSeries | None, str | None]:
    """Decode ``band_data.data`` into per-minute steps, distance and calories.

    Returns ``(steps, distance, calories, layout)``. ``distance``/``calories``
    are ``None`` when no byte channel matches the known daily totals - the
    caller then derives them from the step series instead of inventing numbers.

    ``width_hint`` is ``summary.byteLength`` when the payload carries it: the
    record width really does differ between devices (3 bytes/minute on some
    firmware, 8 on others), so the hint is tried first and the rest of the
    strides are still searched.
    """
    if not raw:
        return None, None, None, None

    # A one-byte-per-minute blob is the simplest case: steps only.
    if len(raw) in (1440, 1380, 1500):
        return (
            MinuteSeries(day, [float(v) for v in raw], "1 byte/minute"),
            None,
            None,
            "1 byte/minute",
        )

    targets = {
        "steps": float(total_steps) if total_steps else None,
        "distance": float(total_distance) if total_distance else None,
        "calories": float(total_calories) if total_calories else None,
    }
    if targets["steps"] is None:
        # Without a daily total there is nothing to calibrate against.
        return None, None, None, None

    # Never trust a divisor alone: a blob is one *record* per minute, so the
    # record width is len(raw) / minutes-of-the-day. Strides that satisfy that
    # are tried first, widest first; only then does the search relax, which
    # keeps a 3-byte record from being read as a 1-byte record whose bytes
    # happen to sum to the same daily total.
    divisors = [s for s in range(1, 33) if len(raw) % s == 0]
    per_minute_widths = [s for s in divisors if len(raw) // s in (1440, 1380, 1500)]
    ordered: list[int] = []
    if width_hint and width_hint in per_minute_widths:
        ordered.append(width_hint)
    ordered.extend(sorted((s for s in per_minute_widths if s not in ordered), reverse=True))
    ordered.extend(sorted((s for s in divisors if s not in ordered), reverse=True))

    best: tuple[float, int, int] | None = None
    for stride in ordered:
        if len(raw) < stride * 60:
            continue
        for offset, channel in enumerate(_channels(raw, stride)):
            error = _best_match(channel, targets["steps"], _STEPS_TOLERANCE)
            if error is None:
                continue
            if best is None or error < best[0]:
                best = (error, stride, offset)
        if best is not None and (best[0] <= 1e-9 or len(raw) // stride in (1440, 1380, 1500)):
            # A one-record-per-minute width produced an exact match: stop here
            # rather than letting a narrower, accidental match win on tie-break.
            break

    if best is None:
        _LOGGER.debug(
            "No channel of a %d byte activity blob sums to %d steps; the day will "
            "be imported from the daily total only",
            len(raw),
            total_steps,
        )
        return None, None, None, None

    _, stride, steps_offset = best
    channels = _channels(raw, stride)
    steps = MinuteSeries(day, [float(v) for v in channels[steps_offset]], f"{stride} bytes/minute, byte {steps_offset}")

    distance: MinuteSeries | None = None
    calories: MinuteSeries | None = None
    used: set[int] = {steps_offset}

    # Distance is usually one of the remaining byte channels. Some firmware
    # stores it in decametres, so accept a match either against the metre total
    # or against a tenth of it.
    for name, target, scale in (
        ("distance", targets["distance"], 1.0),
        ("calories", targets["calories"], 1.0),
    ):
        pick: tuple[float, int] | None = None
        for offset, channel in enumerate(channels):
            if offset in used:
                continue
            if target is None:
                continue
            for factor in (1.0, 10.0, 100.0):
                scaled = [v * factor for v in channel]
                error = _best_match(scaled, target, _TOTAL_TOLERANCE)
                if error is None:
                    continue
                if pick is None or error < pick[0]:
                    pick = (error, offset)
        if pick is None:
            continue
        offset = pick[1]
        used.add(offset)
        series = MinuteSeries(day, [float(v) for v in channels[offset]], f"{stride} bytes/minute, byte {offset}")
        if name == "distance":
            distance = series
        else:
            calories = series

    return steps, distance, calories, steps.layout


def decode_heart_rate(raw: bytes, day: date) -> MinuteSeries | None:
    """Decode ``band_data.data_hr`` into one bpm value per minute."""
    if not raw:
        return None
    values: list[float | None] = []
    for byte in raw:
        if byte in HR_SENTINELS or not (HR_VALID_MIN <= byte <= HR_VALID_MAX):
            values.append(None)
        else:
            values.append(float(byte))
    if not any(v is not None for v in values):
        return None
    return MinuteSeries(day, values, "1 byte/minute")


def parse_band_day(
    item: dict[str, Any],
    tz: ZoneInfo,
) -> tuple[ActivityDay | None, SleepNight | None, list[SleepNight]]:
    """Turn one ``band_data`` record into decoded day, night and naps."""
    raw_day = item.get("date_time")
    if not raw_day:
        return None, None, []
    try:
        day = date.fromisoformat(str(raw_day)[:10])
    except ValueError:
        return None, None, []

    summary = b64_json(item.get("summary"))
    stp = summary.get("stp") or {}
    steps_total = _int_or_none(stp.get("ttl")) or 0
    distance_total = _int_or_none(stp.get("dis")) or 0
    calories_total = _int_or_none(stp.get("cal")) or 0

    activity = ActivityDay(
        day=day,
        steps_total=steps_total,
        distance_m=distance_total,
        calories=calories_total,
        step_goal=_int_or_none(summary.get("goal")),
        summary=summary,
        device_id=item.get("device_id") or item.get("sn"),
        synced_at=_int_or_none(summary.get("sync")),
    )

    width_hint = _int_or_none(summary.get("byteLength"))
    steps, distance, calories, layout = decode_activity(
        b64_bytes(item.get("data")),
        day,
        steps_total,
        distance_total,
        calories_total,
        width_hint=width_hint if width_hint and width_hint <= 32 else None,
    )
    if steps is None:
        steps = steps_from_segments(stp, day, tz)
        layout = "summary.stp.stage segments" if steps else None

    activity.steps = steps
    activity.distance = distance
    activity.calories_minute = calories
    activity.heart_rate = decode_heart_rate(b64_bytes(item.get("data_hr")), day)
    activity.layout = layout

    sleep = decode_sleep(summary, day, tz)
    naps = decode_naps(summary, day, tz)
    if sleep is not None:
        activity.sleep_score = sleep.score
    return activity, sleep, naps


def steps_from_segments(
    stp: dict[str, Any],
    day: date,
    tz: ZoneInfo,
    minutes: int = 1440,
) -> MinuteSeries | None:
    """Rebuild a per-minute step series from ``stp.stage[]`` segments.

    ``stage[]`` carries explicit ``{start, stop, mode, step, dis, cal}``
    segments in minutes from the *same* day's local midnight, ``stop``
    inclusive. Some firmware populates it and leaves the binary blob unusable,
    which makes this the exact fallback rather than a proportional guess.
    """
    segments = stp.get("stage")
    if not isinstance(segments, list) or not segments:
        return None
    values: list[float | None] = [0.0] * minutes
    filled = False
    for entry in segments:
        if not isinstance(entry, dict):
            continue
        start = _int_or_none(entry.get("start"))
        stop = _int_or_none(entry.get("stop"))
        step = _int_or_none(entry.get("step"))
        if start is None or stop is None or step is None:
            continue
        span = max(1, stop - start + 1)
        # Segments carry a total, not a distribution: spread it evenly inside
        # the segment instead of dumping it on the first minute.
        per_minute = step / span
        for minute in range(start, min(stop + 1, minutes)):
            values[minute] = (values[minute] or 0.0) + per_minute
        filled = True
    if not filled:
        return None
    return MinuteSeries(day, values, "summary.stp.stage segments")


# --------------------------------------------------------------------------- #
# sleep
# --------------------------------------------------------------------------- #
def decode_sleep(summary: dict[str, Any], day: date, tz: ZoneInfo) -> SleepNight | None:
    """Decode the ``slp`` block of a band_data summary into one night.

    ``stage[]`` offsets are minutes from the local midnight of the day *before*
    ``day`` (a night spans two dates) and ``stop`` is inclusive.
    """
    slp = summary.get("slp") or {}
    if not slp:
        return None

    start_ts = slp.get("st")
    end_ts = slp.get("ed")
    stages = slp.get("stage") or []
    # A day can carry an empty slp (st == ed, no stages) when the night has not
    # been uploaded yet. That is not a night of zero minutes.
    if not stages and (not start_ts or not end_ts or end_ts <= start_ts):
        return None

    reference = local_midnight(day - timedelta(days=1), tz)
    deep = light = rem = awake = 0
    wake_count = 0
    in_awake = False
    decoded_stages: list[tuple[int, int, int]] = []

    for entry in stages:
        try:
            start = int(entry["start"])
            stop = int(entry["stop"])
            mode = int(entry["mode"])
        except (KeyError, TypeError, ValueError):
            continue
        duration = max(0, stop - start + 1)
        start_ts_stage = int((reference + timedelta(minutes=start)).timestamp())
        decoded_stages.append((start_ts_stage, duration * 60, mode))
        if mode == SLEEP_MODE_DEEP:
            deep += duration
        elif mode == SLEEP_MODE_LIGHT:
            light += duration
        elif mode == SLEEP_MODE_REM:
            rem += duration
        elif mode == SLEEP_MODE_AWAKE:
            awake += duration
            if not in_awake:
                wake_count += 1
                in_awake = True
            continue
        in_awake = False

    if start_ts and end_ts and end_ts <= start_ts:
        start_ts = None
        end_ts = None

    if start_ts is None and decoded_stages:
        start_ts = decoded_stages[0][0]
        end_ts = decoded_stages[-1][0] + decoded_stages[-1][1]

    resting = as_float(slp.get("rhr"))
    if resting is not None and not (25 <= resting <= 120):
        resting = None

    return SleepNight(
        day=day,
        start_ts=int(start_ts or 0),
        end_ts=int(end_ts or 0),
        score=_int_or_none(slp.get("ss")),
        deep_min=deep or _int_or_none(slp.get("dp")),
        light_min=light or _int_or_none(slp.get("lt")),
        # ``dt`` carries REM minutes on firmware that reports it; the stage
        # list is authoritative when present.
        rem_min=rem or _int_or_none(slp.get("dt")),
        # ``wk`` has been reported both as "minutes awake" and as "wake count".
        # The stage list settles it: derive both, fall back to the raw fields.
        awake_min=awake or _int_or_none(slp.get("wk")),
        wake_count=wake_count or _int_or_none(slp.get("wc")),
        resting_hr=int(resting) if resting is not None else None,
        stages=decoded_stages,
    )


def decode_naps(summary: dict[str, Any], day: date, tz: ZoneInfo) -> list[SleepNight]:
    """Decode ``slp.odd_stage`` into separate naps.

    Segments of one nap are adjacent; distinct naps are separated by real gaps.
    Splitting on an awake segment instead would break a single nap into three.
    """
    segments = (summary.get("slp") or {}).get("odd_stage") or []
    if not segments:
        return []
    reference = local_midnight(day - timedelta(days=1), tz)
    ordered = sorted(
        (s for s in segments if isinstance(s.get("start"), int)),
        key=lambda s: s["start"],
    )
    groups: list[list[dict[str, Any]]] = []
    for entry in ordered:
        if groups and entry.get("start", 0) - groups[-1][-1].get("stop", 0) <= 5:
            groups[-1].append(entry)
        else:
            groups.append([entry])

    naps: list[SleepNight] = []
    for group in groups:
        minutes = {SLEEP_MODE_LIGHT: 0, SLEEP_MODE_DEEP: 0, SLEEP_MODE_REM: 0, SLEEP_MODE_AWAKE: 0}
        first = int(group[0]["start"])
        last = int(group[-1].get("stop", first))
        for entry in group:
            try:
                minutes[int(entry["mode"])] = minutes.get(int(entry["mode"]), 0) + (
                    int(entry["stop"]) - int(entry["start"]) + 1
                )
            except (KeyError, TypeError, ValueError):
                continue
        naps.append(
            SleepNight(
                day=day,
                start_ts=int((reference + timedelta(minutes=first)).timestamp()),
                end_ts=int((reference + timedelta(minutes=last + 1)).timestamp()),
                deep_min=minutes[SLEEP_MODE_DEEP],
                light_min=minutes[SLEEP_MODE_LIGHT],
                rem_min=minutes[SLEEP_MODE_REM],
                awake_min=minutes[SLEEP_MODE_AWAKE],
            )
        )
    return naps


def _int_or_none(value: Any) -> int | None:
    number = as_float(value)
    if number is None:
        return None
    return int(number)


# --------------------------------------------------------------------------- #
# events endpoints
# --------------------------------------------------------------------------- #
def parse_samples(
    items: Iterable[dict[str, Any]],
    value_keys: tuple[str, ...] = ("value",),
    *,
    anchor_key: str = "timestamp",
    offset_key: str | None = None,
    unit: str = "ms",
    field: str | None = None,
    base64_field: bool = False,
) -> list[Sample]:
    """Generic sample extractor for the events API.

    Handles the three shapes the API uses: absolute epoch milliseconds per
    sample, an offset from an anchor, and a dense base64 byte array anchored at
    the item timestamp.
    """
    samples: list[Sample] = []
    for item in items:
        value = item.get("value") or {}
        anchor = item.get(anchor_key)
        if base64_field and field:
            raw = b64_bytes(value.get(field) or item.get(field))
            if not raw or anchor is None:
                continue
            base = int(anchor) // 1000 if unit == "ms" else int(anchor)
            for index, byte in enumerate(raw):
                if byte == 0:
                    continue
                samples.append(Sample(base + index * 60, float(byte)))
            continue

        entries = value.get("samples")
        if entries is None and isinstance(value.get("data"), list):
            entries = value["data"]
        if entries is None:
            # Flat payload: the measurement lives directly on the item.
            for key in value_keys:
                number = as_float(item.get(key) if key in item else value.get(key))
                if number is not None and anchor is not None:
                    samples.append(Sample(int(anchor) // 1000, number))
                    break
            continue

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            number = None
            for key in value_keys:
                number = as_float(entry.get(key))
                if number is not None:
                    break
            if number is None:
                continue
            if offset_key and offset_key in entry and anchor is not None:
                divisor = 1000 if unit == "ms" else 1
                ts = int(anchor) // divisor + int(entry[offset_key]) // divisor
            elif "time" in entry:
                raw_time = entry["time"]
                ts = int(raw_time) // 1000 if int(raw_time) > 10_000_000_000 else int(raw_time)
            elif anchor is not None:
                ts = int(anchor) // 1000 if unit == "ms" else int(anchor)
            else:
                continue
            samples.append(Sample(ts, number))
    return samples


def parse_pai(items: Iterable[dict[str, Any]], tz: ZoneInfo) -> list[tuple[date, float, float | None]]:
    """Decode ``PaiHealthInfo`` into ``(local_day, daily_pai, total_pai)``.

    ``timestamp`` is local midnight expressed in UTC, so the date has to be
    taken in the user's timezone or every value lands on the previous day.
    """
    from datetime import datetime as _dt  # noqa: PLC0415

    rows: list[tuple[date, float, float | None]] = []
    for item in items:
        anchor = item.get("timestamp")
        if anchor is None:
            continue
        day = _dt.fromtimestamp(int(anchor) / 1000, tz).date()
        daily = as_float(item.get("dailyPai"))
        total = as_float(item.get("totalPai"))
        if daily is None and total is None:
            continue
        rows.append((day, daily if daily is not None else (total or 0.0), total))
    return rows


def parse_stress(items: Iterable[dict[str, Any]]) -> tuple[list[Sample], dict[date, dict[str, float]]]:
    """Decode ``all_day_stress``: sparse samples plus per-day aggregates.

    ``item.data`` is a *string* containing a JSON array of ``{time, value}``
    with absolute epoch milliseconds; gaps in the app's chart are literally
    minutes with no sample.
    """
    samples: list[Sample] = []
    per_day: dict[date, dict[str, float]] = {}
    for item in items:
        raw = item.get("data")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = None
        if isinstance(raw, list):
            for entry in raw:
                if not isinstance(entry, dict):
                    continue
                number = as_float(entry.get("value"))
                ts = entry.get("time")
                if number is None or ts is None:
                    continue
                if not (0 < number <= 100):
                    continue
                samples.append(Sample(int(ts) // 1000, number))
        anchor = item.get("timestamp")
        if anchor is None:
            continue
        day = datetime.fromtimestamp(int(anchor) / 1000, timezone.utc).date()
        summary = per_day.setdefault(day, {})
        for key, short in (("avgStress", "avg"), ("minStress", "min"), ("maxStress", "max")):
            number = as_float(item.get(key))
            if number is not None:
                summary[short] = number
    return samples, per_day


def parse_spo2(items: Iterable[dict[str, Any]]) -> list[Sample]:
    """Decode ``blood_oxygen`` events.

    The shape has moved between firmware versions: some payloads put the reading
    on the item or ``value``, the older ones bury it in an ``extra`` JSON string
    as ``spo2_decrease`` or as a list of samples. All of them are accepted, and
    physically impossible values are dropped (a SpO2 of 0 is "no reading", not a
    desaturation).
    """
    samples: list[Sample] = []

    def _push(ts: float, value: Any) -> None:
        number = as_float(value)
        if number is None or not (50 <= number <= 100):
            return
        samples.append(Sample(int(ts) // 1000, number))

    for item in items:
        anchor = item.get("timestamp")
        value = item.get("value") or {}

        for holder in (value, item):
            for key in ("spo2_decrease", "spo2", "value"):
                if isinstance(holder.get(key), (int, float)) and anchor is not None:
                    _push(anchor, holder[key])

        raw_extra = value.get("extra") or item.get("extra")
        if raw_extra:
            try:
                extra = json.loads(raw_extra) if isinstance(raw_extra, str) else raw_extra
            except ValueError:
                extra = None
            if isinstance(extra, dict):
                if extra.get("spo2_decrease") is not None and anchor is not None:
                    _push(anchor, extra["spo2_decrease"])
                series = extra.get("spo2")
                if isinstance(series, list):
                    for entry in series:
                        if isinstance(entry, dict):
                            ts = entry.get("time") or entry.get("timestamp") or anchor
                            if ts is not None:
                                _push(ts, entry.get("value"))
                        elif anchor is not None:
                            _push(anchor, entry)

        for entry in value.get("samples") or []:
            if isinstance(entry, dict):
                ts = entry.get("time") or entry.get("timestamp") or anchor
                if ts is not None:
                    _push(ts, entry.get("value") or entry.get("spo2"))
    return samples


def parse_readiness(items: Iterable[dict[str, Any]], tz: ZoneInfo) -> list[dict[str, Any]]:
    """Decode ``readiness/watch_score`` into one record per scored day.

    The scored day is ``value.timestamp`` (local midnight of the day being
    scored). ``item.timestamp`` is when Zepp computed the score, which can fall
    on the following day, so it must not be used as the day key.
    """
    by_day: dict[date, dict[str, Any]] = {}
    for item in items:
        sub_type = item.get("subType")
        # ``watch_score_data`` is the raw blob for the same day; only the
        # scored record carries numbers.
        if sub_type and sub_type != "watch_score":
            continue
        value = item.get("value") or {}
        if not value:
            # v1 returns the fields flat on the item, and repeats ``timestamp``
            # with the day anchor last, which is why json keeps that one.
            value = item
        anchor = value.get("timestamp")
        if anchor is None:
            continue
        day = datetime.fromtimestamp(int(anchor) / 1000, tz).date()
        updated = int(item.get("timestamp") or anchor)

        record = {
            "day": day,
            "readiness": _sentinel_none(value.get("rdnsScore"), 255),
            "sleep_hrv": _sentinel_none(value.get("sleepHRV"), 255),
            "sleep_rhr": _sentinel_none(value.get("sleepRHR"), 255),
            "skin_temp_delta": None,
            "ahi": _sentinel_none(value.get("ahiScore"), 255),
            "updated": updated,
        }
        skin = _sentinel_none(value.get("skinTempCalibrated"), 255)
        if skin is not None:
            record["skin_temp_delta"] = round(float(skin) / 100.0, 2)

        existing = by_day.get(day)
        if existing is None or existing["updated"] <= updated:
            by_day[day] = record
    return list(by_day.values())


def _sentinel_none(value: Any, sentinel: int) -> float | None:
    number = as_float(value)
    if number is None or number == sentinel:
        return None
    return number


def parse_biocharge(items: Iterable[dict[str, Any]]) -> list[Sample]:
    """Decode ``Charge/real_data``: one sample per minute.

    ``total == 255`` means "not computed yet" and must not be charted as a
    BioCharge of 255.
    """
    samples: list[Sample] = []
    for item in items:
        value = item.get("value") or {}
        start = value.get("startTime")
        if start is None:
            continue
        for entry in value.get("samples") or []:
            if not isinstance(entry, dict):
                continue
            total = entry.get("total")
            if total is None or total == 255:
                continue
            offset = entry.get("s")
            if offset is None:
                continue
            samples.append(Sample(int((int(start) + int(offset)) // 1000), float(total)))
    return samples


def parse_watch_statistics(payload: dict[str, Any], keys: tuple[str, ...]) -> list[tuple[date, dict[str, float]]]:
    """Decode ``WatchSportStatistics`` containers into ``(day, values)`` pairs."""
    result: list[tuple[date, dict[str, float]]] = []
    for row in payload.get("items") or []:
        raw_day = row.get("dayId") or row.get("day")
        if raw_day is None:
            continue
        day = _parse_day_id(raw_day)
        if day is None:
            continue
        values: dict[str, float] = {}
        for key in keys:
            if key in row:
                number = as_float(row[key])
                if number is not None:
                    values[key] = number
        if values:
            result.append((day, values))
    return result


def _parse_day_id(raw: Any) -> date | None:
    text = str(raw)
    try:
        if len(text) == 8 and text.isdigit():
            return date(int(text[:4]), int(text[4:6]), int(text[6:]))
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def parse_weight(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Decode scale records into plain dicts keyed by the fields we expose."""
    records: list[dict[str, Any]] = []
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        records.append(
            {
                "ts": item.get("generatedTime") or item.get("createTime"),
                "weight": as_float(item.get("weight")),
                "bmi": as_float(item.get("bmi")),
                "body_fat": as_float(item.get("body_fat_rate")),
                "muscle_mass": as_float(item.get("muscle_mass")),
                "body_water": as_float(item.get("body_water_rate")),
                "bone_mass": as_float(item.get("bone_mass")),
            }
        )
    return records


def parse_workouts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Decode the sport history container. Kept deliberately small."""
    workouts = []
    for item in (payload.get("data") or {}).get("summary") or []:
        if not isinstance(item, dict):
            continue
        try:
            trackid = int(item["trackid"])
        except (KeyError, TypeError, ValueError):
            continue
        workouts.append(
            {
                "trackid": trackid,
                "type": item.get("type"),
                "start": trackid,
                "end": int(item.get("end_time") or 0) or None,
                "duration_s": int(float(item.get("run_time") or 0)) or None,
                "avg_hr": as_float(item.get("avg_heart_rate")),
                "max_hr": as_float(item.get("max_heart_rate")),
                "calories": as_float(item.get("calorie")),
                "load": as_float(item.get("exercise_load")),
                "title": (item.get("sport_title") or "").strip() or None,
                "auto_detected": bool(item.get("auto_recognition")),
            }
        )
    workouts.sort(key=lambda w: w["start"], reverse=True)
    return workouts
