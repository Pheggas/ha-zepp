"""Constants for the Zepp (Amazfit) integration."""

DOMAIN = "zepp"

CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_COUNTRY_CODE = "country_code"
CONF_APPTOKEN = "apptoken"
CONF_USERID = "userid"
CONF_CNAME = "cname"
CONF_REGION_HOST = "region_host"
CONF_DEVICE_ID = "device_id"
CONF_DEVICE_NAME = "device_name"
CONF_DEVICE_MAC = "device_mac"
CONF_DEVICE_SN = "device_sn"
CONF_FIRMWARE = "firmware"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_BACKFILL_DAYS = "backfill_days"
CONF_DETAILED_STATS = "detailed_statistics"

# The watch itself only uploads to the phone when it syncs, so polling faster
# than a few minutes mostly returns identical payloads. 5 minutes matches the
# finest resolution Home Assistant can store (short-term statistics).
DEFAULT_SCAN_INTERVAL = 5
MIN_SCAN_INTERVAL = 1
MAX_SCAN_INTERVAL = 120

# Days re-fetched on every poll. Zepp rewrites the current day and occasionally
# back-fills the previous one, so 3 days gives the pipeline a correction window.
LIVE_WINDOW_DAYS = 3

# Days imported into long-term statistics the first time the integration runs.
# The rest of the year is reachable through the "Sync full history" button.
DEFAULT_BACKFILL_DAYS = 45

# How far back 5-minute (short-term) statistics are rewritten. Home Assistant
# keeps short-term statistics for 10 days by default; writing further back is
# wasted work.
SHORT_TERM_DAYS = 10

# Chunk size for historical band_data requests. Larger windows are rejected or
# truncated by the API.
BACKFILL_CHUNK_DAYS = 14

DEFAULT_REGIONS = [
    "https://api-mifit-ru.huami.com",
    "https://api-mifit-de2.zepp.com",
    "https://api-mifit-de2.huami.com",
    "https://api-mifit-us3.zepp.com",
    "https://api-mifit-us2.zepp.com",
    "https://api-mifit.huami.com",
    "https://api-mifit-sg2.huami.com",
    "https://api-mifit-cn3.zepp.com",
    "https://api-mifit-cn2.huami.com",
]

# Event types requested from the events API. Each entry is
# (event_type, sub_type, v2_endpoint).
EVENT_SOURCES: tuple[tuple[str, str | None, bool], ...] = (
    ("readiness", "watch_score", False),
    ("HRVRMSSD", "real_data", True),
    ("hrv_sdnn", "real_data", True),
    ("RespiratoryRate", "real_data", True),
    ("Charge", "real_data", True),
)

# Sensor keys that are imported into long-term statistics as running totals -
# the entity state resets at local midnight, the statistic sum does not.
CUMULATIVE_KEYS = ("steps", "distance", "calories")

# Everything else is imported with mean/min/max per bucket.
MEASUREMENT_KEYS = (
    "heart_rate",
    "stress",
    "spo2",
    "respiratory_rate",
    "readiness",
    "hrv",
    "pai",
    "biocharge",
    "training_load",
    "training_load_7d",
    "vo2max",
    "skin_temp_delta",
)

SLEEP_KEYS = (
    "sleep_score",
    "sleep_duration",
    "deep_sleep",
    "light_sleep",
    "rem_sleep",
    "awake_time",
    "wake_count",
    "sleep_rhr",
)
