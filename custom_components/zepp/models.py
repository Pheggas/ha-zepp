"""Plain data containers shared by the API, decoder and coordinator layers.

Nothing in here imports Home Assistant, so every structure can be built and
asserted in a unit test without a running instance.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass(slots=True)
class MinuteSeries:
    """A per-minute series for one local day.

    ``values`` always has one entry per minute of the local day (1440 on a
    normal day, 1380/1500 on DST days) or is empty when the payload carried no
    data. ``None`` means "the watch recorded nothing", which is different from
    zero.
    """

    day: date
    values: list[float | None]
    # Which byte of the 3-byte activity record carried this series. Kept for
    # diagnostics: it is discovered at runtime, not hard-coded.
    layout: str | None = None

    def __len__(self) -> int:
        return len(self.values)

    def bucket(self, start_minute: int, minutes: int, mode: str = "sum") -> float | None:
        """Aggregate ``minutes`` samples starting at ``start_minute``."""
        chunk = [v for v in self.values[start_minute : start_minute + minutes] if v is not None]
        if not chunk:
            return None
        if mode == "sum":
            return float(sum(chunk))
        if mode == "max":
            return float(max(chunk))
        if mode == "min":
            return float(min(chunk))
        return float(sum(chunk) / len(chunk))


@dataclass(slots=True)
class ActivityDay:
    """One day of band_data, decoded."""

    day: date
    steps_total: int = 0
    distance_m: int = 0
    calories: int = 0
    step_goal: int | None = None
    sleep_score: int | None = None
    steps: MinuteSeries | None = None
    distance: MinuteSeries | None = None
    calories_minute: MinuteSeries | None = None
    heart_rate: MinuteSeries | None = None
    # How the per-minute step series was obtained, for diagnostics.
    layout: str | None = None
    # Raw summary as decoded, kept for diagnostics and for metrics this build
    # does not surface yet.
    summary: dict = field(default_factory=dict)
    device_id: str | None = None
    # Unix seconds when the phone last uploaded this day.
    synced_at: int | None = None


@dataclass(slots=True)
class SleepNight:
    """One night of sleep.

    ``day`` is the band_data ``date_time`` the night was filed under, which is
    the day the sleep *started* (Zepp files by start date, not wake date).
    """

    day: date
    start_ts: int
    end_ts: int
    score: int | None = None
    deep_min: int | None = None
    light_min: int | None = None
    rem_min: int | None = None
    awake_min: int | None = None
    wake_count: int | None = None
    resting_hr: int | None = None
    stages: list[tuple[int, int, int]] = field(default_factory=list)

    @property
    def duration_min(self) -> int:
        counted = sum(v for v in (self.deep_min, self.light_min, self.rem_min) if v)
        if counted:
            return counted
        return max(0, (self.end_ts - self.start_ts) // 60)


@dataclass(slots=True)
class Sample:
    """A single timestamped measurement."""

    ts: int
    value: float


@dataclass(slots=True)
class EventSeries:
    """A sparse or dense series returned by the events endpoints."""

    source: str
    samples: list[Sample] = field(default_factory=list)
    per_day: dict[date, float] = field(default_factory=dict)
    extra: dict = field(default_factory=dict)


@dataclass(slots=True)
class DayBundle:
    """Everything known about one local day, after decoding."""

    day: date
    activity: ActivityDay | None = None
    sleep: SleepNight | None = None
    stress: list[Sample] = field(default_factory=list)
    stress_summary: dict = field(default_factory=dict)
    spo2: list[Sample] = field(default_factory=list)
    respiratory_rate: list[Sample] = field(default_factory=list)
    biocharge: list[Sample] = field(default_factory=list)
    hrv: list[Sample] = field(default_factory=list)
    hrv_sdnn: list[Sample] = field(default_factory=list)
    readiness: dict = field(default_factory=dict)
    pai: float | None = None
    training_load: float | None = None
    training_load_7d: float | None = None
    training_load_optimal: tuple[float | None, float | None] = (None, None)
    vo2max: float | None = None
    weight: dict = field(default_factory=dict)


@dataclass(slots=True)
class DeviceInfoRecord:
    """One watch bound to the account."""

    device_id: str
    name: str
    mac: str
    serial: str
    firmware: str
    source: str | None = None
    auth_key: str | None = None
    bt_mac: str | None = None
    hardware_version: str | None = None
    product_id: str | None = None
    active: bool = False
    last_active: int | None = None
    battery: int | None = None


def local_day(dt: datetime) -> date:
    """Return the wall-clock date of an aware datetime."""
    return dt.date()
