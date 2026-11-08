"""Tests for the statistics row builder.

Home Assistant is not installed in this environment, so the handful of modules
``history.py`` touches are stubbed at import time. That is enough to exercise
the arithmetic that decides what the graphs actually show: cumulative sums,
bucket boundaries, and the daily values.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

TZ = ZoneInfo("Europe/Bratislava")


import ha_stub  # noqa: E402

ha_stub.install()

from custom_components.zepp import history  # noqa: E402
from custom_components.zepp.models import ActivityDay, DayBundle, MinuteSeries, SleepNight  # noqa: E402


def _utc(day: date, hour: int) -> datetime:
    """Local wall-clock hour of a day, as an aware UTC datetime."""
    return datetime(day.year, day.month, day.day, hour, tzinfo=TZ).astimezone(timezone.utc)


def _activity(day: date, steps_by_minute: dict[int, int], total: int) -> ActivityDay:
    values: list[float | None] = [0.0] * 1440
    for minute, steps in steps_by_minute.items():
        values[minute] = float(steps)
    return ActivityDay(
        day=day,
        steps_total=total,
        distance_m=total * 2,
        calories=total // 10,
        steps=MinuteSeries(day, values, "test"),
    )


def test_hourly_cumulative_and_running_sum() -> None:
    day = date(2026, 11, 7)
    # 600 steps in hour 08, 400 in hour 09.
    steps = {8 * 60 + i: 10 for i in range(60)}
    steps.update({9 * 60 + i: 10 for i in range(40)})
    bundle = DayBundle(day=day, activity=_activity(day, steps, 1000))
    now = datetime(2026, 11, 8, 12, 0, tzinfo=TZ)

    rows = history.build_rows([bundle], TZ, minutes=60, now=now, base_sums={"steps": 5000.0})
    steps_rows = rows["steps"]
    # A row per completed hour of the day: a flat 0-step hour is still a real
    # hour and keeps the long-term graph from showing gaps.
    assert len(steps_rows) == 24, len(steps_rows)
    by_start = {row["start"]: row for row in steps_rows}
    eight = by_start[_utc(day, 8)]
    nine = by_start[_utc(day, 9)]
    assert eight["sum"] == 5600.0 and eight["state"] == 600.0
    assert nine["sum"] == 6000.0 and nine["state"] == 1000.0
    # Nothing recorded before 08:00 keeps the running sum where it was.
    assert by_start[_utc(day, 7)]["sum"] == 5000.0


def test_five_minute_buckets() -> None:
    day = date(2026, 11, 7)
    steps = {8 * 60 + i: 5 for i in range(60)}
    bundle = DayBundle(day=day, activity=_activity(day, steps, 300))
    now = datetime(2026, 11, 8, 12, 0, tzinfo=TZ)

    rows = history.build_rows([bundle], TZ, minutes=5, now=now)
    assert len(rows["steps"]) == 288, len(rows["steps"])
    active = [
        row
        for row in rows["steps"]
        if _utc(day, 8) <= row["start"] < _utc(day, 9)
    ]
    assert len(active) == 12
    # ``state`` is the running total inside the local day; each 5-minute row
    # adds 25 steps, which is what the graph plots as a staircase.
    states = [row["state"] for row in active]
    assert states == [25.0 * (i + 1) for i in range(12)], states
    assert rows["steps"][-1]["sum"] == 300.0


def test_heart_rate_hourly_aggregates() -> None:
    day = date(2026, 11, 7)
    activity = _activity(day, {}, 0)
    values: list[float | None] = [None] * 1440
    for minute in range(8 * 60, 9 * 60):
        values[minute] = 60.0 if minute % 2 else 80.0
    activity.heart_rate = MinuteSeries(day, values)
    bundle = DayBundle(day=day, activity=activity)
    now = datetime(2026, 11, 8, 12, 0, tzinfo=TZ)

    rows = history.build_rows([bundle], TZ, minutes=60, now=now)
    hr = rows["heart_rate"]
    assert len(hr) == 1
    assert hr[0]["min"] == 60.0 and hr[0]["max"] == 80.0 and hr[0]["mean"] == 70.0


def test_sleep_lands_on_wake_day() -> None:
    day = date(2026, 11, 6)
    night = SleepNight(
        day=day,
        start_ts=int(datetime(2026, 11, 6, 23, 0, tzinfo=TZ).timestamp()),
        end_ts=int(datetime(2026, 11, 7, 7, 0, tzinfo=TZ).timestamp()),
        score=86,
        deep_min=100,
        light_min=240,
        rem_min=120,
    )
    bundle = DayBundle(day=day, sleep=night)
    now = datetime(2026, 11, 8, 12, 0, tzinfo=TZ)

    rows = history.build_rows([bundle], TZ, minutes=60, now=now)
    assert rows["sleep_score"][0]["start"] == _utc(date(2026, 11, 7), 0)
    assert rows["sleep_score"][0]["state"] == 86.0
    assert rows["sleep_duration"][0]["state"] == 460.0


def test_future_buckets_are_not_written() -> None:
    day = date(2026, 11, 7)
    steps = {10 * 60 + i: 10 for i in range(60)}
    bundle = DayBundle(day=day, activity=_activity(day, steps, 600))
    # 10:30 local on the same day: only the 10:00 hour is complete.
    now = datetime(2026, 11, 7, 10, 30, tzinfo=TZ)
    rows = history.build_rows([bundle], TZ, minutes=60, now=now)
    hours = {row["start"] for row in rows["steps"]}
    # The 10:00 hour is still running: it is written with what is known so far
    # and rewritten by the next poll. Nothing later may appear.
    assert max(hours) == _utc(day, 10), max(hours)
    assert len(hours) == 11


def test_missing_series_falls_back_to_daily_total() -> None:
    day = date(2026, 11, 7)
    activity = ActivityDay(day=day, steps_total=1234, distance_m=0, calories=0)
    bundle = DayBundle(day=day, activity=activity)
    now = datetime(2026, 11, 8, 12, 0, tzinfo=TZ)
    rows = history.build_rows([bundle], TZ, minutes=60, now=now, base_sums={"steps": 0.0})
    assert len(rows["steps"]) == 1
    assert rows["steps"][0]["estimated"] is True
    assert rows["steps"][0]["sum"] == 1234.0


def test_underfilled_series_corrects_running_sum() -> None:
    """Decoded minutes that fall short of the reported total must not drift."""
    day = date(2026, 11, 7)
    steps = {8 * 60 + i: 1 for i in range(10)}  # 10 decoded steps
    bundle = DayBundle(day=day, activity=_activity(day, steps, 500))
    now = datetime(2026, 11, 8, 12, 0, tzinfo=TZ)
    rows = history.build_rows([bundle], TZ, minutes=60, now=now, base_sums={"steps": 0.0})
    assert rows["steps"][-1]["sum"] == 500.0


def test_interval_helper() -> None:
    start = datetime(2026, 11, 7, 0, 0, tzinfo=timezone.utc)
    buckets = list(history.stats.iter_buckets(start, start + timedelta(hours=3), 60))
    assert len(buckets) == 3


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
