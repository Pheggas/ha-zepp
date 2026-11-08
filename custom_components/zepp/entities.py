"""Single source of truth for the entity surface.

The ``key`` of every spec is the unique-id suffix, the entity-id suffix and the
statistics key. Keeping one table means a sensor cannot exist without a
matching statistics definition, and the ``async_resolve_statistic_ids`` lookup
in the coordinator can never drift from the entities that were actually added.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SensorSpec:
    """One sensor."""

    key: str
    name: str
    unit: str | None = None
    device_class: str | None = None
    state_class: str | None = None
    icon: str = "mdi:chart-line"
    # Key in ``coordinator.data`` when it differs from ``key``.
    data_key: str | None = None
    # Imported into statistics as a running total.
    cumulative: bool = False
    # Extra attributes copied from the coordinator payload.
    attributes: tuple[str, ...] = ()

    @property
    def value_key(self) -> str:
        return self.data_key or self.key


# Order matters only for presentation.
SENSORS: tuple[SensorSpec, ...] = (
    # --- activity ---------------------------------------------------- #
    SensorSpec("steps", "Steps", "steps", None, "total_increasing", "mdi:walk", cumulative=True),
    SensorSpec("distance", "Distance", "m", "distance", "total_increasing", "mdi:map-marker-distance", cumulative=True),
    SensorSpec("calories", "Calories", "kcal", None, "total_increasing", "mdi:fire", cumulative=True),
    SensorSpec("step_goal", "Step Goal", "steps", None, None, "mdi:flag-checkered"),
    # --- cardio ------------------------------------------------------ #
    SensorSpec(
        "heart_rate",
        "Heart Rate",
        "bpm",
        None,
        "measurement",
        "mdi:heart-pulse",
        attributes=("hr_min", "hr_max", "hr_avg"),
    ),
    SensorSpec("resting_hr", "Resting Heart Rate", "bpm", None, "measurement", "mdi:heart-box"),
    SensorSpec("hrv", "HRV rMSSD", "ms", None, "measurement", "mdi:sine-wave"),
    # --- wellness ---------------------------------------------------- #
    SensorSpec(
        "stress",
        "Stress Level",
        "score",
        None,
        "measurement",
        "mdi:emoticon-neutral-outline",
        attributes=("stress_min", "stress_max"),
    ),
    SensorSpec("spo2", "Blood Oxygen (SpO2)", "%", None, "measurement", "mdi:water-percent"),
    SensorSpec("respiratory_rate", "Respiratory Rate", "rpm", None, "measurement", "mdi:lungs"),
    SensorSpec("pai", "PAI", "PAI", None, "measurement", "mdi:medal-outline"),
    SensorSpec("biocharge", "BioCharge", "score", None, "measurement", "mdi:battery-heart-variant"),
    SensorSpec(
        "readiness_score",
        "Readiness Score",
        "score",
        None,
        "measurement",
        "mdi:lightning-bolt",
        data_key="readiness",
        attributes=("skin_temp_delta",),
    ),
    SensorSpec("breathing_score", "Breathing Quality", "score", None, "measurement", "mdi:weather-windy", data_key="breathing"),
    # --- sleep ------------------------------------------------------- #
    SensorSpec("sleep_score", "Sleep Score", "score", None, "measurement", "mdi:sleep", attributes=("sleep_night",)),
    SensorSpec("sleep_duration", "Sleep Duration", "min", "duration", "measurement", "mdi:bed-clock"),
    SensorSpec("deep_sleep", "Deep Sleep", "min", "duration", "measurement", "mdi:power-sleep"),
    SensorSpec("light_sleep", "Light Sleep", "min", "duration", "measurement", "mdi:weather-night"),
    SensorSpec("rem_sleep", "REM Sleep", "min", "duration", "measurement", "mdi:brain"),
    SensorSpec("awake_time", "Awake Time", "min", "duration", "measurement", "mdi:eye-outline"),
    SensorSpec("wake_count", "Wake Count", None, None, "measurement", "mdi:alarm-bell"),
    SensorSpec("sleep_rhr", "Sleep Resting Heart Rate", "bpm", None, "measurement", "mdi:heart-pulse"),
    # --- training ---------------------------------------------------- #
    SensorSpec(
        "training_load",
        "Training Load (7-Day)",
        None,
        None,
        "measurement",
        "mdi:weight-lifter",
        data_key="training_load_7d",
        attributes=("training_load_min", "training_load_max"),
    ),
    SensorSpec("training_load_today", "Daily Training Load", None, None, "measurement", "mdi:timer-outline", data_key="training_load"),
    SensorSpec("vo2max", "VO2 Max", "ml/kg/min", None, "measurement", "mdi:run-fast"),
    # --- diagnostic -------------------------------------------------- #
    SensorSpec("last_sync", "Last Cloud Sync", None, "timestamp", None, "mdi:cloud-check-outline"),
    # --- body -------------------------------------------------------- #
    SensorSpec("weight", "Weight", "kg", "weight", "measurement", "mdi:scale-bathroom"),
    SensorSpec("bmi", "BMI", None, None, "measurement", "mdi:human"),
    SensorSpec("body_fat", "Body Fat", "%", None, "measurement", "mdi:percent"),
)

# Statistics that get a long-term series. The self-describing sensors above are
# the ones the graph can chart; the scale and goal values are informational.
STATISTIC_SENSORS: tuple[SensorSpec, ...] = tuple(
    spec for spec in SENSORS if spec.state_class is not None
)

STAT_DEFINITIONS: dict[str, tuple[str | None, bool]] = {
    spec.key: (spec.unit, spec.cumulative) for spec in STATISTIC_SENSORS
}
