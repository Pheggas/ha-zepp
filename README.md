# Zepp (Amazfit) for Home Assistant — high-resolution rework

Home Assistant integration for Amazfit/Zepp watches, using the official Zepp
cloud the phone app already talks to. No extra app on the watch, no BLE bridge,
no second account, and the phone app stays signed in.

This is a rework of [yardeff/ha-zepp](https://github.com/yardeff/ha-zepp). It
keeps the entity surface and the concept; it replaces the data path.

---

## Why the graphs only showed two bumps a day

The watch records per minute. The phone app draws that minute-level detail. The
original integration showed a flat line with one or two steps per day, and the
cause was not the cloud — it was how the payload was read:

| Symptom | Actual cause |
| --- | --- |
| Steps/distance/calories change twice a day | The live sensor was fed `summary.stp.ttl`, the *daily total*. The per-minute series in the same response (`data`) was only read by the backfill button. |
| Hourly history was missing | The incremental hourly writer looked up hard-coded entity ids (`sensor.amazfit_active_steps`). Those ids do not exist on a normal install, so the query returned nothing and the whole routine exited silently every poll. |
| Sleep score changed once every few days | The sleep block started with `if result["sleep_score"] is not None: break`, and `result` is seeded from the previous poll. After the first successful fetch it broke immediately, forever. The value only moved on a restart. |
| Heart rate had no history | Minute values were read, but only `last_valid()` was used for the entity state, and long-term import only ran on a manual button press. |
| Two sets of identical entities | The devices endpoint returned the same watch twice and every platform created entities per entry without deduplication. |

## What this build does instead

* **Decodes the per-minute payloads.** `data` (steps/distance/calories) and
  `data_hr` (heart rate) are unpacked into 1440 minute slots per local day.
* **Writes two resolutions.** One row per hour into long-term statistics
  (kept forever) and one row per 5 minutes into short-term statistics (what the
  history graph reads when you zoom into a day). The 5-minute window is the
  last 10 days, matching the recorder's retention.
* **Polls every 5 minutes** by default instead of 15, so the current day keeps
  up with the cloud as the phone syncs.
* **Re-reads the last 3 days on every poll**, because Zepp corrects recent days
  after the fact. Imports are idempotent: a day always produces the same rows.
* **Fixes the sleep block.** The newest night with a real duration wins; the
  value is keyed by the night, so it advances every morning instead of freezing.
* **Self-calibrating activity decoding.** The binary record width differs
  between watches (3 bytes/minute on some firmware, 8 on others) and the byte
  that holds the step count is not documented. The decoder derives both by
  matching each candidate channel against the day's known total, so it adapts
  instead of assuming an offset.
* **One entity set per watch**, deduplicated on device id.
* **No invented data.** If a series cannot be decoded, the day is imported as
  its daily total at the last hour it covers and `layout` in the diagnostics
  says so. Nothing is smoothed into a plausible-looking curve.

## Resolution you get

| Metric | Cloud holds | Imported as |
| --- | --- | --- |
| Steps, distance, calories | per minute | 5-minute and hourly, cumulative |
| Heart rate | per minute | 5-minute and hourly (mean/min/max) |
| Stress | sparse samples, absolute timestamps | 5-minute and hourly |
| SpO2, respiratory rate, BioCharge | per minute to sparse | 5-minute and hourly |
| Sleep (score, stages, resting HR, wake count) | one record per night, stage segments | daily |
| Readiness, HRV, PAI, training load, VO2max | one value per day | daily |
| Weight / body composition | per scale measurement | daily |

## Install

1. HACS → Integrations → three-dot menu → **Custom repositories**.
2. Add `https://github.com/Pheggas/ha-zepp-v2` as an **Integration**.
3. Install, then restart Home Assistant.
4. Settings → Devices & Services → **Add integration** → *Zepp (Amazfit)*.

Sign in with email and password, or with browser cookies if the account was
created through Google/Apple/Mi. Either way the phone app keeps its session:
the integration signs in with the `com.huami.webapp` client, a separate session
slot on Zepp's side.

On first start it imports `backfill_days` of history (45 by default) in the
background. Press **Sync Full History** for the whole year the cloud still
holds, or call `zepp.sync_history` with `days: 365`.

### Upgrading from the original integration

Entity ids, unique ids and the `zepp.sync_history` service are unchanged, so
existing dashboards and automations keep working and the statistics history is
continued rather than restarted. Two devices that were previously created from
one watch collapse into one; remove the leftover device in the UI.

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| Update interval | 5 min | How often the cloud is polled. |
| Write 5-minute statistics | on | Fills `statistics_short_term` for the last 10 days. This is what makes a day show intra-day detail in the history graph. |
| Days of history to import | 45 | Used on first start and by the backfill button. |

## How the 5-minute rows are written

Long-term rows go through Home Assistant's supported
`recorder.statistics.async_import_statistics`. That API rejects anything not
aligned to the top of an hour, and there is no import API for the 5-minute
table at all — so those rows are written directly into
`statistics_short_term` with the recorder's own SQLAlchemy model, in an
executor, upserting on `(metadata_id, start_ts)`. It is the only way to make
*past* days show minute-level detail; live states alone can never backfill.
Turn the option off and everything else keeps working, with hourly resolution.

## Verifying what your account actually holds

```bash
python3 tools/zepp_probe.py --apptoken <apptoken> --userid <id> --days 5
```

It dumps every endpoint's raw response and prints a report: how many bytes the
activity blob has, which channel matched the daily total, how many valid heart
rate minutes exist, and which event sources returned data. Useful when a watch
model behaves differently, and it is what the decoder was validated against.
Only `aiohttp` is required.

## Diagnostics

Settings → Devices & Services → Zepp → three-dot menu → **Download
diagnostics** reports, per source, whether it answered, the activity layout that
was detected, and the backfill state. Credentials are redacted.

## Notes and limitations

* Everything still depends on the phone syncing to Zepp. The cloud cannot show
  what was never uploaded; the integration re-reads recent days so late uploads
  land in the right place.
* The private API is undocumented and can change without notice.
* Minute slots are treated as wall-clock minutes of the local day, which keeps
  a 1440-slot payload aligned on the two DST days a year.
* Sleep is filed by Zepp under the date the night started, not the morning you
  woke up. The statistics row is written on the wake day, which is what a
  "sleep score for today" is expected to mean.

## Credits

Original integration and the `com.huami.webapp` login approach:
[yardev](https://github.com/yardeff). Endpoint research from
[J-solan/zepp-dashboard](https://github.com/J-solan/zepp-dashboard) and
[EvanCooke/zepp-export](https://github.com/EvanCooke/zepp-export); see
[docs/zepp-api.md](docs/zepp-api.md) for the consolidated map.

MIT licensed. Not affiliated with Zepp Health or Huami.
