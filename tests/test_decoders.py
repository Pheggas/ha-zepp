"""Decoder tests. Run with ``python3 tests/test_decoders.py`` - no pytest needed.

The fixtures are synthetic on purpose: the real payloads from a live account
contain health data that does not belong in a git repository. What matters for
these tests is that the *structure* is the one the API actually returns, which
was confirmed against live payloads.
"""
from __future__ import annotations

import base64
import json
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ha_stub  # noqa: E402

ha_stub.install()

from custom_components.zepp import decoders  # noqa: E402

TZ = ZoneInfo("Europe/Bratislava")


def _summary(**overrides) -> str:
    payload = {
        "goal": 8000,
        "sync": 1730140000,
        "stp": {"ttl": 1000, "dis": 750, "cal": 40},
        "slp": {},
    }
    payload.update(overrides)
    return base64.b64encode(json.dumps(payload).encode()).decode()


def _steps_blob(width: int, steps_offset: int, total: int, dis_offset=None, dis_total=0, cal_offset=None, cal_total=0) -> bytes:
    """Build a blob whose channel at ``steps_offset`` sums exactly to ``total``."""
    minutes = 1440
    per_minute = total // minutes
    remainder = total - per_minute * minutes
    raw = bytearray(width * minutes)
    for minute in range(minutes):
        value = per_minute + (1 if minute < remainder else 0)
        raw[minute * width + steps_offset] = min(value, 255)
    if dis_offset is not None:
        per = dis_total // minutes
        for minute in range(minutes):
            raw[minute * width + dis_offset] = min(per, 255)
    if cal_offset is not None:
        per = cal_total // minutes
        for minute in range(minutes):
            raw[minute * width + cal_offset] = min(per, 255)
    return bytes(raw)


def test_three_byte_layout() -> None:
    """The layout the upstream integration assumed: byte 2 is the step count."""
    total = 9000
    raw = _steps_blob(3, 2, total)
    steps, _, _, layout = decoders.decode_activity(raw, date(2026, 11, 7), total, 0, 0)
    assert steps is not None, "3-byte layout must calibrate"
    assert sum(steps.values) == total
    assert layout and "byte 2" in layout, layout


def test_eight_byte_layout() -> None:
    """A device that packs 8 bytes per minute must still be decoded correctly."""
    total = 8272
    raw = _steps_blob(8, 5, total)
    steps, _, _, layout = decoders.decode_activity(raw, date(2026, 11, 7), total, 0, 0)
    assert steps is not None, "8-byte layout must calibrate"
    assert sum(steps.values) == total
    assert layout and "byte 5" in layout, layout


def test_width_hint_is_honoured() -> None:
    """summary.byteLength narrows the search to the real record width."""
    total = 4321
    raw = _steps_blob(8, 3, total)
    steps, _, _, layout = decoders.decode_activity(
        raw, date(2026, 11, 7), total, 0, 0, width_hint=8
    )
    assert steps is not None and sum(steps.values) == total
    assert "8 bytes" in (layout or ""), layout


def test_distance_and_calories_channels() -> None:
    # Per-minute byte channels must divide evenly into the minute count.
    raw = _steps_blob(3, 2, 6000, dis_offset=0, dis_total=4320, cal_offset=1, cal_total=1440)
    steps, distance, calories, _ = decoders.decode_activity(raw, date(2026, 11, 7), 6000, 4320, 1440)
    assert steps is not None and distance is not None and calories is not None
    assert sum(distance.values) == 4320
    assert sum(calories.values) == 1440


def test_uncalibratable_blob_returns_none() -> None:
    raw = bytes((i * 37) % 256 for i in range(11520))
    steps, distance, calories, layout = decoders.decode_activity(raw, date(2026, 11, 7), 8272, 3545, 222)
    assert steps is None and distance is None and calories is None and layout is None


def test_stage_segments_fallback() -> None:
    stp = {
        "ttl": 900,
        "stage": [
            {"start": 600, "stop": 609, "mode": 3, "step": 300},
            {"start": 700, "stop": 719, "mode": 3, "step": 600},
        ],
    }
    series = decoders.steps_from_segments(stp, date(2026, 11, 7), TZ)
    assert series is not None
    assert sum(series.values) == 900
    assert series.values[600] == 30.0
    assert series.values[719] == 30.0
    assert series.values[650] == 0.0


def test_heart_rate_sentinels() -> None:
    raw = bytes([0, 254, 255, 60, 12, 250, 61]) + bytes(1440 - 7)
    series = decoders.decode_heart_rate(raw, date(2026, 11, 7))
    assert series is not None
    assert series.values[3] == 60.0
    assert series.values[0] is None  # 0 is "not measured"
    assert series.values[1] is None  # 254 is "not measured"
    assert series.values[4] is None  # 12 bpm is not a reading
    assert series.values[5] is None  # 250 bpm is out of range
    assert series.values[6] == 61.0


def test_sleep_stage_durations_are_stop_inclusive() -> None:
    summary = {
        "slp": {
            "st": 1730030000,
            "ed": 1730060000,
            "dp": 60,
            "lt": 140,
            "dt": 30,
            "ss": 81,
            "rhr": 50,
            "stage": [
                {"start": 1440, "stop": 1509, "mode": 4},  # 70 light
                {"start": 1510, "stop": 1569, "mode": 5},  # 60 deep
                {"start": 1570, "stop": 1599, "mode": 8},  # 30 REM
                {"start": 1600, "stop": 1669, "mode": 4},  # 70 light
                {"start": 1670, "stop": 1674, "mode": 7},  # 5 awake
            ],
        }
    }
    night = decoders.decode_sleep(summary, date(2026, 11, 7), TZ)
    assert night is not None
    assert night.score == 81
    assert night.deep_min == 60
    assert night.light_min == 140
    assert night.rem_min == 30
    assert night.awake_min == 5
    assert night.wake_count == 1
    assert night.resting_hr == 50
    # stage[] is anchored to the midnight of the day before date_time
    # start=1440 is 24:00 of the reference day (the day before date_time)
    assert datetime.fromtimestamp(night.stages[0][0], TZ) == datetime(2026, 11, 7, 0, 0, tzinfo=TZ)


def test_empty_sleep_is_not_a_night() -> None:
    assert decoders.decode_sleep({"slp": {"st": 1, "ed": 1, "ss": 0, "rhr": 0}}, date(2026, 11, 7), TZ) is None
    assert decoders.decode_sleep({"slp": {}}, date(2026, 11, 7), TZ) is None


def test_dst_spring_forward() -> None:
    """2026-03-29 is 23 hours long in Europe/Bratislava."""
    midnight = decoders.local_midnight(date(2026, 3, 29), TZ)
    assert midnight.utcoffset().total_seconds() == 3600
    noon = datetime.fromtimestamp(decoders.minute_timestamp(date(2026, 3, 29), 720, TZ), TZ)
    # Index 720 is wall clock 12:00 on every day: the payload has a fixed
    # number of slots, so the offset is wall-clock, not elapsed time.
    assert noon.hour == 12 and noon.minute == 0, noon


def test_stress_samples_parse_json_string() -> None:
    day = date(2026, 10, 28)
    anchor_ms = int(datetime(day.year, day.month, day.day, tzinfo=ZoneInfo("UTC")).timestamp() * 1000)
    item = {
        "timestamp": anchor_ms,
        "avgStress": "40.5",
        "minStress": "12",
        "maxStress": "88",
        "data": json.dumps([{"time": anchor_ms + 60000, "value": 35}, {"time": anchor_ms + 120000, "value": 41}]),
    }
    samples, per_day = decoders.parse_stress([item])
    assert len(samples) == 2 and samples[0].value == 35.0
    assert per_day[day]["avg"] == 40.5


def test_readiness_uses_value_timestamp_as_day() -> None:
    tz = TZ
    day_ms = int(datetime(2026, 11, 6, tzinfo=tz).timestamp() * 1000)
    items = [
        {
            "timestamp": day_ms + 8 * 3600 * 1000,  # computed the next morning
            "value": {"timestamp": day_ms, "rdnsScore": 95, "sleepHRV": 61, "skinTempCalibrated": 12, "ahiScore": 100},
        }
    ]
    records = decoders.parse_readiness(items, tz)
    assert len(records) == 1
    assert records[0]["day"] == date(2026, 11, 6)
    assert records[0]["readiness"] == 95
    assert records[0]["skin_temp_delta"] == 0.12


def test_biocharge_skips_uncomputed_total() -> None:
    items = [
        {
            "value": {
                "startTime": 1730073600000,
                "samples": [
                    {"s": 0, "total": 55, "mental": 50, "physical": 60},
                    {"s": 60000, "total": 255, "mental": 51, "physical": 61},
                ],
            }
        }
    ]
    samples = decoders.parse_biocharge(items)
    assert len(samples) == 1 and samples[0].value == 55.0


def test_respiratory_dense_blob() -> None:
    raw = bytes([0] * 60 + [16] * 60 + [0] * 1320)
    items = [{"timestamp": 1730073600000, "value": {"measurements": base64.b64encode(raw).decode()}}]
    samples = decoders.parse_samples(
        items, ("measurements",), anchor_key="timestamp", unit="ms", field="measurements", base64_field=True
    )
    assert len(samples) == 60
    assert samples[0].ts == 1730073600 + 60 * 60
    assert samples[0].value == 16.0


def test_real_fixture_heart_rate() -> None:
    """The sanitised fixture keeps a realistic HR shape and all sentinels."""
    path = Path(__file__).resolve().parents[2] / "zepp-dashboard/ingest/tests/fixtures/band_data_day.json"
    if not path.exists():
        print("  (fixture not present, skipped)")
        return
    payload = json.loads(path.read_text())
    item = payload["data"][0]
    series = decoders.decode_heart_rate(decoders.b64_bytes(item["data_hr"]), date(2026, 11, 7))
    assert series is not None
    valid = [v for v in series.values if v is not None]
    assert len(valid) > 1200
    assert all(25 <= v <= 240 for v in valid)


def main() -> int:
    failures = 0
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_") and callable(fn)]
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except AssertionError as err:
            failures += 1
            print(f"FAIL {name}: {err}")
        except Exception as err:  # noqa: BLE001
            failures += 1
            print(f"ERR  {name}: {type(err).__name__}: {err}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
