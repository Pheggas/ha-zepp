#!/usr/bin/env python3
"""Probe the Zepp cloud API against a real account and report what it holds.

This is the instrument used to verify the integration: it asks every endpoint
the integration knows about, dumps the raw payloads to ``--out`` and decodes
them with the same code the integration uses. The report answers, per metric,
"what resolution does this account actually have".

It deliberately loads the decoders without importing Home Assistant, so it runs
on a laptop with nothing but ``aiohttp`` installed.

    python3 tools/zepp_probe.py --apptoken UQVB... --userid 7007417683 --days 5

Nothing is uploaded anywhere; the dumps stay in the directory you point it at.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import importlib
import json
import sys
import types
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

COMPONENT_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "zepp"


def load_pure_modules():
    """Import the API and decoder modules without executing the package init.

    ``custom_components/zepp/__init__.py`` imports Home Assistant; registering a
    synthetic parent package with the same directory as its path lets the
    relative imports inside ``api.py`` and ``decoders.py`` resolve without
    running it.
    """
    sys.path.insert(0, str(COMPONENT_DIR.parent))
    package = types.ModuleType("zepp_pure")
    package.__path__ = [str(COMPONENT_DIR)]  # type: ignore[attr-defined]
    sys.modules["zepp_pure"] = package
    api = importlib.import_module("zepp_pure.api")
    decoders = importlib.import_module("zepp_pure.decoders")
    const = importlib.import_module("zepp_pure.const")
    return api, decoders, const


api, decoders, const = load_pure_modules()

import aiohttp  # noqa: E402


def _jwt_userid(apptoken: str) -> str | None:
    """Try to read a user id out of the token itself, when it is a JWT."""
    parts = apptoken.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        blob = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:  # noqa: BLE001
        return None
    for key in ("user_id", "userid", "uid", "sub"):
        if blob.get(key):
            return str(blob[key])
    return None


async def probe(args: argparse.Namespace) -> int:
    tz = ZoneInfo(args.timezone)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    raw_dir = out / "raw"
    raw_dir.mkdir(exist_ok=True)

    userid = args.userid or _jwt_userid(args.apptoken)
    if not userid:
        print("No --userid given and the token is not a readable JWT.", file=sys.stderr)
        return 2

    lines: list[str] = []
    report: dict[str, object] = {}

    def say(text: str = "") -> None:
        print(text)
        lines.append(text)

    say("# Zepp API probe")
    say()
    say(f"- generated: {datetime.now(tz).isoformat(timespec='seconds')}")
    say(f"- user id: `{userid}`")
    say(f"- timezone: `{tz.key}`")
    say()

    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        client = api.ZeppClient(session, args.apptoken, userid, args.host or "")
        if not args.host:
            host, devices = await client.async_probe_hosts(None)
            client.host = host
        else:
            devices = await client.async_get_devices()
        say(f"- region host: `{client.host}`")
        say()

        parsed = api.parse_devices(devices)
        say("## Devices")
        say()
        say("| device_id | name | source | firmware | active | battery |")
        say("| --- | --- | --- | --- | --- | --- |")
        for device in parsed:
            say(
                f"| `{device['device_id']}` | {device['device_name']} | "
                f"`{device.get('device_source')}` | {device.get('firmware') or '-'} | "
                f"{device.get('active_status')} | {device.get('battery')} |"
            )
        say()
        report["devices"] = parsed
        (raw_dir / "devices.json").write_text(json.dumps(devices, indent=2))

        # ---- band data ---------------------------------------------------- #
        today = datetime.now(tz).date()
        start = today - timedelta(days=args.days - 1)
        items = await client.async_get_band_data(start.isoformat(), today.isoformat(), detail=True)
        (raw_dir / "band_data.json").write_text(json.dumps(items, indent=2))

        say(f"## Band data ({start} .. {today})")
        say()
        say(f"Returned **{len(items)}** daily records.")
        say()
        say("| day | data bytes | bytes/min | byteLength | step channel | decoded steps | reported ttl | match | HR bytes | valid HR | sleep | stages |")
        say("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")

        decoded_days = {}
        for item in items:
            activity, night, naps = decoders.parse_band_day(item, tz)
            if activity is None:
                continue
            decoded_days[activity.day] = activity
            summary = activity.summary or {}
            raw_activity = decoders.b64_bytes(item.get("data"))
            raw_hr = decoders.b64_bytes(item.get("data_hr"))
            steps_sum = (
                int(round(sum(v for v in activity.steps.values if v is not None)))
                if activity.steps
                else 0
            )
            match = abs(steps_sum - activity.steps_total) <= max(2, activity.steps_total * 0.02)
            hr_valid = (
                sum(1 for v in activity.heart_rate.values if v is not None)
                if activity.heart_rate
                else 0
            )
            say(
                f"| {activity.day} | {len(raw_activity)} | "
                f"{len(raw_activity) / 1440:.2f} | {summary.get('byteLength')} | "
                f"{activity.layout or '-'} | {steps_sum} | {activity.steps_total} | "
                f"{'yes' if match else 'NO'} | {len(raw_hr)} | {hr_valid} | "
                f"{'yes' if night else '-'} | {len(night.stages) if night else 0} |"
            )
        say()

        if decoded_days:
            days = sorted(decoded_days)
            steps_series = decoded_days[days[-1]].steps
            if steps_series:
                nonzero = sum(1 for v in steps_series.values if v)
                say(
                    f"Latest day has **{nonzero}** minutes with steps, "
                    f"{sum(1 for v in steps_series.values if v is not None)} minutes with any value."
                )
                say()

        # ---- events ------------------------------------------------------- #
        say("## Event endpoints")
        say()
        say("| source | endpoint | items | samples | newest | first payload keys |")
        say("| --- | --- | --- | --- | --- | --- |")
        per_day_sources = {"all_day_stress", "readiness", "PaiHealthInfo", "Charge",
                           "HRVRMSSD", "RespiratoryRate"}

        async def probe_event(name, event_type, sub_type, v2, decoder, raw_name):
            # One request per source: these endpoints ignore date ranges and
            # return newest-first, so the limit is the only knob.
            limit = 10 if event_type in per_day_sources else 150
            try:
                if v2:
                    payload = await client.async_get_v2_events(
                        event_type, sub_type=sub_type, limit=limit
                    )
                else:
                    payload = await client.async_get_user_events(
                        event_type, sub_type=sub_type, limit=limit
                    )
            except Exception as err:  # noqa: BLE001
                say(f"| {name} | `{event_type}` | error: {err} | | |")
                return
            (raw_dir / f"{raw_name}.json").write_text(json.dumps(payload, indent=2))
            samples = decoder(payload) if decoder else []
            keys = ""
            if payload:
                first = payload[0].get("value") if isinstance(payload[0].get("value"), dict) else {}
                keys = ", ".join(list(payload[0].keys())[:6])
                if first:
                    keys += " / " + ", ".join(list(first.keys())[:6])
            endpoint = "v2/users/me" if v2 else "users/{id}"
            newest = max((int(i.get("timestamp") or 0) for i in payload), default=0)
            newest_text = (
                datetime.fromtimestamp(newest / 1000, tz).strftime("%Y-%m-%d %H:%M")
                if newest
                else "-"
            )
            say(f"| {name} | `{endpoint}` | {len(payload)} | {len(samples)} | {newest_text} | {keys} |")

        await probe_event(
            "stress", "all_day_stress", None, False,
            lambda p: decoders.parse_stress(p)[0], "stress",
        )
        await probe_event(
            "spo2", "blood_oxygen", None, False, decoders.parse_spo2, "spo2",
        )
        await probe_event(
            "readiness", "readiness", "watch_score", True,
            lambda p: decoders.parse_readiness(p, tz), "readiness",
        )
        await probe_event(
            "hrv rmssd", "HRVRMSSD", "real_data", True,
            lambda p: decoders.parse_samples(p, ("hrv", "rmssd", "value"), offset_key="s"),
            "hrv",
        )
        await probe_event(
            "respiratory", "RespiratoryRate", "real_data", True,
            lambda p: decoders.parse_samples(
                p, ("measurements",), anchor_key="timestamp", unit="ms",
                field="measurements", base64_field=True,
            ),
            "respiratory",
        )
        await probe_event(
            "biocharge", "Charge", "real_data", True, decoders.parse_biocharge, "biocharge",
        )
        await probe_event(
            "pai", "PaiHealthInfo", None, False,
            lambda p: [item for item in p if item.get("totalPai") is not None], "pai",
        )
        say()

        # ---- daily containers --------------------------------------------- #
        say("## Daily containers")
        say()
        try:
            payload = await client.async_get_watch_statistics(
                "SPORT_LOAD", (today - timedelta(days=30)).isoformat(), today.isoformat()
            )
            (raw_dir / "sport_load.json").write_text(json.dumps(payload, indent=2))
            rows = decoders.parse_watch_statistics(payload, ("currnetDayTrainLoad", "wtlSum"))
            say(f"- `WatchSportStatistics/SPORT_LOAD`: {len(payload.get('items') or [])} rows, {len(rows)} parseable")
        except Exception as err:  # noqa: BLE001
            say(f"- `WatchSportStatistics/SPORT_LOAD`: error {err}")

        try:
            payload = await client.async_get_watch_statistics(
                "VO2_MAX", (today - timedelta(days=30)).isoformat(), today.isoformat()
            )
            (raw_dir / "vo2max.json").write_text(json.dumps(payload, indent=2))
            say(f"- `WatchSportStatistics/VO2_MAX`: {len(payload.get('items') or [])} rows")
        except Exception as err:  # noqa: BLE001
            say(f"- `WatchSportStatistics/VO2_MAX`: error {err}")

        try:
            records = await client.async_get_weight_records(limit=10)
            (raw_dir / "weight.json").write_text(json.dumps(records, indent=2))
            say(f"- `weightRecords`: {len(records)} records")
        except Exception as err:  # noqa: BLE001
            say(f"- `weightRecords`: error {err}")

        try:
            workouts = await client.async_get_sport_history()
            (raw_dir / "workouts.json").write_text(json.dumps(workouts, indent=2))
            parsed_workouts = decoders.parse_workouts({"data": {"summary": workouts}})
            say(f"- `sport/run/history`: {len(parsed_workouts)} workouts")
        except Exception as err:  # noqa: BLE001
            say(f"- `sport/run/history`: error {err}")
        say()

        # ---- what this means ---------------------------------------------- #
        say("## Resolution actually available on this account")
        say()
        latest = decoded_days[max(decoded_days)] if decoded_days else None
        say(f"- steps: {'per minute' if latest and latest.steps else 'daily total only'}")
        say(f"- heart rate: {'per minute' if latest and latest.heart_rate else 'not present in band_data'}")
        say("- active energy / distance: per minute when the activity blob carries their channels, otherwise derived from the step distribution")
        say("- sleep: one record per night with stage segments (start/stop in minutes)")
        say("- stress: sparse samples with absolute timestamps")
        say()

    (out / "probe-report.md").write_text("\n".join(lines) + "\n")
    (out / "probe-report.json").write_text(json.dumps(report, indent=2, default=str))
    print(f"\nWrote {out / 'probe-report.md'} and raw payloads under {raw_dir}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apptoken", required=True, help="Zepp app token (apptoken header/cookie)")
    parser.add_argument("--userid", default=None, help="numeric Zepp user id")
    parser.add_argument("--host", default=None, help="regional host; probed when omitted")
    parser.add_argument("--days", type=int, default=5, help="how many days of band data to fetch")
    parser.add_argument("--timezone", default="Europe/Bratislava")
    parser.add_argument("--out", default="probe-out")
    return asyncio.run(probe(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
