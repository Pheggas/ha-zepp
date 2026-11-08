"""Async client for the Zepp (Huami) cloud API.

The API is undocumented. Everything here was assembled from the endpoints the
upstream ``ha-zepp`` integration already used plus the endpoint shapes other
reverse-engineering projects have confirmed against live accounts - see
``docs/zepp-api.md`` in this repository for the sources.

Three rules shape this module:

* Login as ``com.huami.webapp``, read as ``com.huami.midong``. Zepp keeps one
  session per app name, so logging in with the phone app's name kicks the phone
  out of its session.
* A 401 means the app token is gone; there is no refresh endpoint. The caller
  re-authenticates (email/password) or asks the user for fresh cookies.
* A wrong regional host answers 403 or an empty body instead of an error, so
  host discovery probes and compares rather than trusting the first answer.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.parse
from typing import Any

import aiohttp

from .const import DEFAULT_REGIONS

_LOGGER = logging.getLogger(__name__)

# Fixed channel id the phone app sends; the API accepts it for every account.
ZEPP_CHANNEL = "a100900101016"

# Headers for data reads. ``appname`` selects the namespace being read (the
# phone app's), while the session itself belongs to ``com.huami.webapp``.
_APP_TOKEN_HEADER = "apptoken"
_READ_HEADERS = {
    "appname": "com.huami.midong",
    "appplatform": "android_phone",
    "v": "2.0",
    "vn": "9.12.5",
    "timezone": "UTC",
    "user-agent": "Zepp/9.12.5",
}

_RETRY_STATUS = {429, 500, 502, 503, 504}


class ZeppError(Exception):
    """Base error for this integration."""


class ZeppAuthError(ZeppError):
    """The app token is missing, expired or rejected."""


class ZeppConnectionError(ZeppError):
    """The cloud could not be reached."""


class ZeppRateLimitError(ZeppConnectionError):
    """Too many requests - back off before retrying."""


def sanitize_host(host: str) -> str:
    """Normalise a regional host and repair the retired US endpoint."""
    host = (host or "").strip().rstrip("/")
    if host and not host.startswith("http"):
        host = f"https://{host}"
    if "api-mifit-us2.zepp.com" in host:
        return "https://api-mifit-us3.zepp.com"
    return host


def candidate_hosts(cname: str | None) -> list[str]:
    """Build the host probe list, most specific first."""
    hosts: list[str] = []
    for part in (cname or "").split(","):
        part = part.strip()
        if part and "api-mifit" in part:
            host = sanitize_host(part)
            if host not in hosts:
                hosts.append(host)
    for host in DEFAULT_REGIONS:
        host = sanitize_host(host)
        if host not in hosts:
            hosts.append(host)
    return hosts


# --------------------------------------------------------------------------- #
# authentication
# --------------------------------------------------------------------------- #
async def async_login(
    session: aiohttp.ClientSession,
    email: str,
    password: str,
    country_code: str = "AUTO",
) -> dict[str, Any]:
    """Exchange email/password for an app token using the web client.

    The web client id keeps the phone's session alive; that is the whole reason
    this flow is used instead of the phone app's.
    """
    step1_url = f"https://api-user.huami.com/registrations/{urllib.parse.quote(email)}/tokens"
    payload = {
        "password": password,
        "client_id": "HuaMi",
        "token": "access",
        "state": "REDIRECTION",
        "redirect_uri": "https://s3-us-west-2.amazonaws.com/hm-registration/successsignin.html",
        "json_response": "true",
        "app_name": "com.huami.webapp",
    }
    if country_code and country_code.upper() != "AUTO":
        payload["country_code"] = country_code.upper()

    try:
        async with session.post(
            step1_url,
            data=payload,
            headers={
                "app_name": "com.huami.webapp",
                "Accept-Language": "en-US,en;q=0.9",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status == 429:
                raise ZeppRateLimitError("Zepp login rate-limited (HTTP 429)")
            if resp.status != 200:
                raise ZeppConnectionError(f"Zepp login failed with HTTP {resp.status}")
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError) as err:
        raise ZeppConnectionError(f"Could not reach the Zepp login endpoint: {err}") from err

    access_token = data.get("access")
    if not access_token:
        raise ZeppAuthError(data.get("message") or "Zepp rejected the credentials")

    region = (
        data.get("country_code")
        or (country_code.upper() if country_code and country_code.upper() != "AUTO" else None)
        or _region_from_name(data.get("region"))
        or "US"
    )
    return await async_exchange_token(session, access_token, region)


async def async_exchange_token(
    session: aiohttp.ClientSession,
    access_token: str,
    country_code: str = "AUTO",
) -> dict[str, Any]:
    """Turn a raw access token, cookie string or JSON blob into an app token."""
    token = (access_token or "").strip()
    extracted = _extract_from_cookie_string(token)
    if extracted:
        return extracted

    if "access=" in token:
        try:
            parsed = urllib.parse.urlparse(token)
            query = urllib.parse.parse_qs(parsed.query or parsed.fragment)
            if "access" in query:
                token = query["access"][0]
        except ValueError:
            pass

    region = country_code.upper() if country_code and country_code.upper() != "AUTO" else "US"
    payload = {
        "country_code": region,
        "app_name": "com.huami.webapp",
        "third_name": "huami",
        "grant_type": "access_token",
        "code": token,
        "device_id": "02:00:00:00:00:00",
        "device_model": "web",
        "app_version": "4.3.0",
        "allow_registration": "false",
        "dn": "account.huami.com,api-user.huami.com,api-mifit.huami.com,auth.huami.com,api-open.huami.com",
    }
    try:
        async with session.post(
            "https://account.huami.com/v2/client/login",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status == 429:
                raise ZeppRateLimitError("Zepp token exchange rate-limited (HTTP 429)")
            if resp.status != 200:
                raise ZeppConnectionError(f"Zepp token exchange failed with HTTP {resp.status}")
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError) as err:
        raise ZeppConnectionError(f"Could not reach the Zepp account endpoint: {err}") from err

    if data.get("result") != "ok":
        raise ZeppAuthError(f"Zepp login failed: {data.get('error_code') or data.get('message')}")

    token_info = data.get("token_info") or {}
    app_token = token_info.get("app_token")
    if not app_token:
        raise ZeppAuthError("Zepp returned no app token")

    return {
        "apptoken": app_token,
        "userid": str(token_info.get("user_id")),
        "cname": _cnames(data.get("domains")),
    }


def _extract_from_cookie_string(raw: str) -> dict[str, Any] | None:
    """Accept the JSON/``document.cookie`` blob the config flow lets users paste."""
    if not raw:
        return None
    if raw.startswith("{") and raw.endswith("}"):
        try:
            blob = json.loads(raw)
        except ValueError:
            blob = {}
        token = blob.get("apptoken") or blob.get("app_token")
        user = blob.get("userid") or blob.get("user_id")
        if token and user:
            return {"apptoken": token, "userid": str(user), "cname": blob.get("cname", "")}

    lowered = raw.lower()
    if "apptoken=" not in lowered and "app_token=" not in lowered:
        return None
    token = user = None
    for chunk in raw.replace("\n", ";").split(";"):
        if "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        key = key.strip().lower().replace("-", "_")
        value = value.strip().strip('"').strip("'")
        if key in ("apptoken", "app_token"):
            token = value
        elif key in ("userid", "user_id"):
            user = value
    if token and user:
        return {"apptoken": token, "userid": str(user), "cname": ""}
    return None


def _cnames(domains: Any) -> str:
    names: list[str] = []
    if isinstance(domains, list):
        for entry in domains:
            if not isinstance(entry, dict):
                continue
            for name in entry.get("cnames") or []:
                if name:
                    names.append(name)
                    break
    return ",".join(names)


def _region_from_name(region: Any) -> str | None:
    text = str(region or "").lower()
    for needle, code in (("us", "US"), ("eu", "DE"), ("de", "DE"), ("ru", "RU"), ("cn", "CN"), ("sg", "SG")):
        if needle in text:
            return code
    return None


def parse_devices(raw_devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalise the device payloads into the records the platforms consume.

    Duplicates are collapsed on MAC/device id: the endpoint occasionally returns
    the same watch twice, and creating it twice used to double every entity.
    """
    from .device_catalog import resolve_device_name

    parsed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for device in raw_devices:
        if device.get("bindingStatus", 1) != 1:
            continue
        mac = str(device.get("macAddress") or "")
        device_id = str(device.get("deviceId") or mac.replace(":", "") or device.get("sn") or "")
        if not device_id or device_id in seen:
            continue
        seen.add(device_id)

        additional: dict[str, Any] = {}
        raw_additional = device.get("additionalInfo")
        if isinstance(raw_additional, str):
            try:
                additional = json.loads(raw_additional)
            except ValueError:
                additional = {}
        elif isinstance(raw_additional, dict):
            additional = raw_additional

        battery = None
        for source in (device.get("additionalSource"), device.get("additionalInfo")):
            if not source:
                continue
            try:
                blob = json.loads(source) if isinstance(source, str) else source
            except ValueError:
                continue
            if isinstance(blob, dict) and isinstance(blob.get("battery"), dict):
                level = blob["battery"].get("level")
                if level is not None:
                    battery = int(level)
                    break

        parsed.append(
            {
                "device_id": device_id,
                "device_name": resolve_device_name(
                    device.get("deviceSource"),
                    device.get("displayName") or (device.get("deviceInfo") or {}).get("name"),
                ),
                "device_mac": mac or "Unknown",
                "device_sn": device.get("sn") or "",
                "firmware": device.get("firmwareVersion") or "",
                "device_source": device.get("deviceSource"),
                "auth_key": additional.get("auth_key"),
                "bt_mac": additional.get("btmac"),
                "hardware_version": additional.get("hardwareVersion"),
                "product_id": additional.get("productId"),
                "active_status": device.get("activeStatus", 1 if len(raw_devices) == 1 else 0),
                "last_active_time": device.get("lastActiveStatusUpdateTime")
                or device.get("lastStatusUpdateTime"),
                "battery": battery,
            }
        )

    parsed.sort(
        key=lambda d: (d.get("active_status", 0), d.get("last_active_time") or 0),
        reverse=True,
    )
    return parsed


# --------------------------------------------------------------------------- #
# client
# --------------------------------------------------------------------------- #
class ZeppClient:
    """Thin, retrying wrapper over the regional data hosts."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        apptoken: str,
        userid: str,
        host: str,
        *,
        concurrency: int = 4,
    ) -> None:
        self._session = session
        self.apptoken = apptoken
        self.userid = str(userid)
        self.host = sanitize_host(host)
        self._semaphore = asyncio.Semaphore(concurrency)
        self.last_error: str | None = None

    # -- plumbing ---------------------------------------------------------- #
    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        host: str | None = None,
        timeout: int = 30,
        attempts: int = 3,
    ) -> dict[str, Any]:
        url = f"{sanitize_host(host or self.host)}{path}"
        headers = dict(_READ_HEADERS)
        headers[_APP_TOKEN_HEADER] = self.apptoken
        clean = {k: str(v) for k, v in (params or {}).items() if v is not None}

        last_exc: Exception | None = None
        for attempt in range(attempts):
            async with self._semaphore:
                try:
                    async with self._session.get(
                        url,
                        headers=headers,
                        params=clean,
                        timeout=aiohttp.ClientTimeout(total=timeout),
                    ) as resp:
                        if resp.status == 401:
                            raise ZeppAuthError(f"Zepp rejected the app token for {path}")
                        if resp.status in _RETRY_STATUS:
                            if resp.status == 429:
                                raise ZeppRateLimitError(f"Rate limited on {path}")
                            raise ZeppConnectionError(f"HTTP {resp.status} from {path}")
                        if resp.status == 403:
                            raise ZeppConnectionError(f"HTTP 403 from {path} (wrong regional host?)")
                        if resp.status != 200:
                            raise ZeppConnectionError(f"HTTP {resp.status} from {path}")
                        return await resp.json(content_type=None)
                except ZeppAuthError:
                    raise
                except (ZeppConnectionError, aiohttp.ClientError, TimeoutError, ValueError) as err:
                    last_exc = err
                    self.last_error = str(err)
                    if attempt + 1 < attempts:
                        await asyncio.sleep(1.5 * (attempt + 1))
        raise ZeppConnectionError(str(last_exc) if last_exc else f"{path} failed")

    # -- discovery --------------------------------------------------------- #
    async def async_probe_hosts(self, cname: str | None = None) -> tuple[str, list[dict[str, Any]]]:
        """Find the regional host that owns this account's devices."""
        hosts = candidate_hosts(cname or self.host)

        async def probe(host: str) -> tuple[str, list[dict[str, Any]]]:
            try:
                devices = await self.async_get_devices(host=host)
            except ZeppError:
                return host, []
            return host, devices

        results = await asyncio.gather(*(probe(host) for host in hosts))
        for host, devices in results:
            bound = [d for d in devices if d.get("bindingStatus", 1) == 1]
            if bound:
                return sanitize_host(host), bound
        for host, devices in results:
            if devices:
                return sanitize_host(host), devices
        return self.host, []

    async def async_get_devices(self, host: str | None = None) -> list[dict[str, Any]]:
        payload = await self._get(
            f"/users/{self.userid}/devices",
            {
                "userid": self.userid,
                "channel": ZEPP_CHANNEL,
                "device": "android_32",
                "device_type": "android_phone",
                "enableMultiDevice": "true",
                "enableMultiDeviceOnMultiType": "true",
                "v": "2.0",
            },
            host=host,
            timeout=20,
        )
        items = payload.get("items")
        return items if isinstance(items, list) else []

    # -- health data ------------------------------------------------------- #
    async def async_get_band_data(
        self,
        from_date: str,
        to_date: str,
        *,
        detail: bool = True,
        device_type: str = "0",
    ) -> list[dict[str, Any]]:
        """Daily records with the minute-level blobs (``query_type=detail``)."""
        payload = await self._get(
            "/v1/data/band_data.json",
            {
                "userid": self.userid,
                "from_date": from_date,
                "to_date": to_date,
                "query_type": "detail" if detail else "summary",
                "device_type": device_type,
            },
            timeout=45,
        )
        data = payload.get("data")
        return data if isinstance(data, list) else []

    async def async_get_user_events(
        self,
        event_type: str,
        *,
        limit: int = 200,
        sub_type: str | None = None,
        reverse: bool = True,
    ) -> list[dict[str, Any]]:
        """Newest-first events from the user-scoped endpoint.

        ``from``/``to`` are deliberately never sent: on this API they do not
        filter, they empty the response. ``reverse=true`` plus ``limit`` is the
        shape that actually returns recent records, and the caller filters by
        timestamp afterwards.
        """
        payload = await self._get(
            f"/users/{self.userid}/events",
            {
                "eventType": event_type,
                "userId": self.userid,
                "subType": sub_type,
                "limit": limit,
                "reverse": "true" if reverse else None,
            },
        )
        items = payload.get("items")
        return items if isinstance(items, list) else []

    async def async_get_v2_events(
        self,
        event_type: str,
        *,
        sub_type: str | None = None,
        limit: int = 200,
        reverse: bool = True,
    ) -> list[dict[str, Any]]:
        """Newest-first events from the account-scoped endpoint. See above."""
        payload = await self._get(
            "/v2/users/me/events",
            {
                "eventType": event_type,
                "subType": sub_type,
                "limit": limit,
                "reverse": "true" if reverse else None,
            },
        )
        items = payload.get("items")
        return items if isinstance(items, list) else []

    async def async_get_watch_statistics(
        self,
        statistic: str,
        start_day: str,
        end_day: str,
        *,
        limit: int = 60,
    ) -> dict[str, Any]:
        return await self._get(
            f"/v2/watch/users/{self.userid}/WatchSportStatistics/{statistic}",
            {"startDay": start_day, "endDay": end_day, "limit": limit, "isReverse": "1"},
        )

    async def async_get_weight_records(
        self,
        *,
        member_id: str = "-1",
        from_ts: int | None = None,
        to_ts: int | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        payload = await self._get(
            f"/users/{self.userid}/members/{member_id}/weightRecords",
            {"limit": limit, "isForward": "0", "fromTime": from_ts, "toTime": to_ts},
        )
        items = payload.get("items")
        return items if isinstance(items, list) else []

    async def async_get_sport_history(self, limit: int = 30) -> list[dict[str, Any]]:
        """Workout list. ``source`` is a fixed literal, not a filter."""
        payload = await self._get(
            "/v1/sport/run/history.json",
            {"source": "run.mifit.huami.com", "startTrackId": 0, "stopTrackId": int(time.time())},
        )
        return ((payload.get("data") or {}).get("summary")) or []
