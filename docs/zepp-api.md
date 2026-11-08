# Zepp / Huami cloud API — engineering reference
Unofficial map of the API the Zepp phone app uses, for a Home Assistant integration
reading an **Amazfit T-Rex 3 Pro 48mm/44mm** (firmware `6.3.25.7`) on a **RU**
account: `user_id` `7007417683`, data host `https://api-mifit-ru.huami.com`. The
phone syncs ~2×/day, so the cloud lags up to ~12 h and historical data is the
product, not "now".

> **§A at the end of this document is a live verification log** from an
> Amazfit T-Rex 3 Pro (firmware 6.4.16.3) on a `-de2`-registered account served
> by `api-mifit-ru.huami.com`. Where §A contradicts a claim below, §A wins: it
> was measured on real responses, not read from another project.

**Not official, not supported, no stability promise.** Every claim is tagged with
a source in §1.2; anything not verified against real data is tagged `unverified`
and listed in §10.4. Read §7 (time anchoring) and §9 (sleep) before writing a
parser — the payloads are not wrong, they are anchored differently, and the field
that looks like a timezone is the one that lies.

---

## 1. Scope, status, sourcing
### 1.1 In scope
| In | Out |
|---|---|
| `api-user-*` / `api-mifit-*` account API used by the mobile app | Zepp **Open Platform** (`devopen.zepp.com`), a separate contractual API |
| Read paths for health + workout data | BLE / Gadgetbridge device access |
| Token acquisition (password login, cookie reuse) | Anything that writes to the account |
| Payload decoding, anchoring, sentinels | What a score *means* (product semantics) |

### 1.2 Sources, and what each is authoritative for
Blocks below carry a tag like `[S1 · docs/zepp-api.md]`.

| ID | Repo | Files read | Strongest for |
|---|---|---|---|
| **S1** | `J-solan/zepp-dashboard` | `docs/zepp-api.md`, `docs/01-login.md`, `docs/02-extract.md`, `ingest/{auth,client,parsers,run}.py`, `tools/probe_events.py`, `ingest/tests/fixtures/*` | AES login; `data_hr`; sleep stages/naps; BioCharge; stress; readiness; respiratory; local-vs-UTC midnight distinctions |
| **S2** | `EvanCooke/zepp-export` | `docs/api-reference.md`, `docs/mapping-guide.md`, `zepp_export/{client,decoders}.py` | `band_data.summary` field inventory; `appPlatform`/encryption interaction; `exertion`, `phn` shapes |
| **S3** | `tkuennen/health-data-hub` | `plugins/sources/amazfit/{README.md,fetch.py,normalize.py,__init__.py}` | `/users/{id}/heartRate`; SpO2 `extra`; regional host drift; "plaintext login is dead" datapoint |
| **S4** | `hrishikeshmane/zepp-mcp` | `huami_client.py`, `zepp_sleep.py`, `zepp_health_events.py`, `zepp_sports.py`, `README.md` | Event inventory (SpO2/PAI/Charge/readiness/HRV); `wk` vs `wc`; workout-detail decoders |
| **S5** | `drfittri/zepp-mcp` | `huami_client.py` | Upstream of S4; identical client, no health events |
| **S6** | `m4ary/zepp-health-cli` | `zepp_health.py`, `README.md` | Exact preset literals; `events/dateString`; `bloodPressure`; `manualData`; skin-temp sentinels |
| **S7** | `DhavalBhimani44/zepp-mcp` | `docs/api-findings.md`, `docs/capability-matrix.md`, `zepp_mcp/{auth,events,decode,codes,body}.py` | Login redirect metadata; per-minute steps in `data`; sentinel families; `weightRecords`; verification levels |
| **S8** | `argrento/huami-token` | `huami_token/{constants,zepp}.py` | Canonical login constants, header sets, `logout` |
| **S9** | `alan13367/amazfit-swift` | `docs/data-access.md` | Cookie/`apptoken` fallback; "do not forward tokens across regions" |
| **S10** | `docs-v2.vitalera.io` (vendor doc) | Amazfit integration page | A third regional-host table (`-cn`, `-de` on `huami.com`); lowest trust |

Pointers not used as evidence beyond pagination: `bentasker/zepp_to_influxdb`,
`rolandsz` exporter, `H3llK33p3r/zepp-fit-extractor` (all cited by S7).

### 1.3 Verification levels
Adapted from `[S7 · docs/capability-matrix.md]`, the only source that states a
confidence per fact.

| Level | Meaning |
|---|---|
| **L1** | Verified live, cross-checked against a second quantity |
| **L2** | Verified against a recorded payload by an independent quantity in it |
| **L3** | Corroborated by ≥2 independent clients; never checked against data |
| **L4** | Hypothesis read off the payload; needs a capture |
| **L5** | Probed and inaccessible, or unresolved |

A field name in exactly one repo is **L3 at best**; two repos copying each other
is one source.

### 1.4 Envelope shapes — know which one you parse
| Envelope | Endpoints | Test |
|---|---|---|
| `{code, message, data:[…]}` | `band_data.json`, `sport/run/history.json`, `huami.health.getUserInfo.json` | `code == 1` = success `[S2]` |
| `{items:[…]}` | all `/events`, `WatchSportStatistics`, `heartRate`, `weightRecords`, `fileInfo` | presence of `items` |
| bare list / `{data:{…}}` | `sport/run/detail.json` | `data` may be a dict of packed strings |

Testing only `payload["data"]` for emptiness reports every `/v2/users/me/events`
response as empty — a real bug recorded in `[S7 · docs/api-findings.md]`.

---

## 2. Authentication
Two phases, plus a cookie shortcut.

### 2.1 Phase 1 — credentials → access code
`POST https://{auth_host}/v2/registrations/tokens`

- Body: urlencoded form, **AES-128-CBC** encrypted, PKCS7, key `xeNtBVqzDc6tuNTh`,
  IV `MAAAYAAAAAAAAABg`. Published protocol constants, not secrets
  `[S8 · constants.py]`.
- `x-hm-ekv: 1` header is required `[S1 · ingest/auth.py]`.
- Fields: `emailOrPhone`, `password`, `state=REDIRECTION`, `client_id=HuaMi`,
  `redirect_uri=https://s3-us-west-2.amazonaws.com/hm-registration/successsignin.html`,
  `region=us-west-2`, `token=["access","refresh"]`, `country_code=US`.
- Answer is **303**; everything is in the `Location` query `[S7 · auth.py]`:

```
…/successsignin.html?state=REDIRECTION&region=us-west-2
  &refresh=<REDACTED>&access=<REDACTED>
  &country_code=IN&expiration=<epoch_s>&attempts=<n>&max_attempts=10
```

| Field | Meaning | Note |
|---|---|---|
| `access` | one-time code for phase 2 | not the token data calls use |
| `refresh` | refresh token | **no consumer exists** in S1–S8 (§10.2) |
| `country_code` | account's real country | may differ from what you sent |
| `expiration` | lifetime of the access **code** | ≈10 days observed; not the app-token TTL |
| `attempts`/`max_attempts` | failed-login counter, shared across hosts **and regions** | 10 failures presumably locks the account |
| `error` | present instead of `access` | bad credentials |

Two hard rules `[S7]`: the `region` body field is **ignored** at step 1 (five
"regional variants" were five identical requests), and **never loop logins** —
the counter is shared, so a retry loop walks the account into a lockout.

### 2.2 Phase 2 — access code → app token
`POST https://{login_host}/v2/client/login`, form-encoded.

| Field | S1 / S8 | S7 |
|---|---|---|
| `code` | `access` value | same |
| `device_id` | fresh `uuid4()` per login | **stable** id from disk |
| `device_model`, `app_version` | `android_phone`, `9.12.5` | same |
| `dn` | `api-mifit.zepp.com,api-user.zepp.com,api-mifit.zepp.com,api-watch.zepp.com,app-analytics.zepp.com,auth.zepp.com,api-analytics.zepp.com` | shorter list, same idea |
| `third_name`, `source` | `huami`, `com.huami.watch.hmwatchmanager:9.12.5:151689` | same |
| **`app_name`** | **`com.huami.webapp`** | **`com.xiaomi.hm.health`** |
| `country_code`, `grant_type`, `allow_registration`, `lang` | `US`, `access_token`, `false`, `en` | caller-supplied |

```json
{"result":"ok",
 "token_info":{"app_token":"<REDACTED>","user_id":"7007417683",
               "login_token":"<REDACTED>","app_ttl":2592000},
 "regist_info":{"country_code":"RU"},
 "domains":[{"host":"api-mifit.zepp.com","cnames":["api-mifit-ru.huami.com"]}]}
```

`app_ttl` is the app token's lifetime (2592000 s = 30 days). `ttl`, when present,
belongs to `login_token` and is longer — do not read the wrong one
`[S7 · auth.py]`. `domains[]` is the authoritative way to learn your data host
(§3.2).

### 2.3 `app_name` / `appname` — the rule that bites
Zepp keys a session by `(user, app_name)`. Phase-2 `app_name` picks your session
slot; the `appname` header on a GET picks the namespace you read. GETs do not
register a session, so a GET never evicts the phone `[S1 · docs/01-login.md]`,
`[S7 · auth.py]` (their issue #5).

| Where | Header / field | Value | Effect |
|---|---|---|---|
| Phase-2 login | `app_name` body | `com.huami.webapp` (S1) / `com.xiaomi.hm.health` (S7) | separate slot; phone keeps syncing |
| Phase-2 login | `app_name` body | `com.huami.midong` | **evicts the phone**: sync and live HR stop until the app is reopened. This is `huami-token`'s shipped default `[S8]` |
| Data GET | `appname` header | `com.huami.midong` | full namespace, incl. workout history |
| Data GET | `appname` header | `com.huami.webapp` | workout history **empty**; other data still readable |
| Data GET | `appname` header | `com.xiaomi.hm.health` | works (S2, S7); namespace not compared against midong |

Sources disagree on the login literal. `com.xiaomi.hm.health` is the retired Mi
Fit client id, i.e. the identity least likely to collide with a shipping app
slot `[S7]`; use it first. **Neither was verified on this RU account.** Failure
mode is the phone silently losing sync; reopening the Zepp app restores it.
Phase-1's `app_name` header is `com.huami.midong` in S1 and S8 with no observed
eviction (it is a header on the token endpoint, not a session registration).

### 2.4 Data-request headers and `r=`
```
apptoken: <app_token>          # REQUIRED
appname: com.huami.midong      # REQUIRED (namespace)
appplatform: ios_phone         # convention; web also works
v: 2.0   vn: 10.2.5   cv: 1722_10.2.5   vb: 202604132257
user-agent: Zepp/10.2.5 (iPhone; iOS 26.3.1; Scale/3.00)
lang: en
timezone: Europe/Bratislava    # advisory only, never an anchor (§7.3)
accept: */*
x-request-id: <uuid4>          # S3/S7 send it, S1 does not
```

plus query parameter `r=<uuid4>` on **every** request (uppercase in S1,
lowercase elsewhere; S3 says the backend requires it). `[S1 · client.py]`,
`[S3 · fetch.py]`, `[S7]`

**Encryption is triggered by `x-hm-ekv: 1`, not by `appplatform` alone.** S2's
table blames `appPlatform: ios_phone`, but S1 and S6 send `ios_phone` *without*
`x-hm-ekv` and get plain JSON. Rule (L4): omit `x-hm-ekv` on data GETs and you
get JSON; send it and you get `application/octet-stream`.

### 2.5 Lifetime, 401, 429, failures
| Situation | Behaviour | Handling | Src |
|---|---|---|---|
| Normal | `app_ttl` 30 days | cache token + `user_id` + timestamp to disk, mode 600 | S7, S1 |
| Observed | 11–25 days | treat 30 d as an upper bound; S3 hedges to 25 | S3 |
| `401` | expired/revoked; **no refresh path** | one full re-login per process behind a lock, then fail | S1 |
| Stale token on `200` | body has `code 0`, `message:"invalid token"`, `data.code=="0102"` | detect and re-login once — it is not an HTTP error | S4 |
| `429` | login and data rate limits | exponential backoff **with jitter** (parallel GETs must not retry in lockstep), cap ~10 s, honour numeric `Retry-After` | S1, S4 |
| `5xx` | stray 502/503 are routine on a 30-day backfill | retry 3× on `{429,500,502,503,504}` + transport errors; let 4xx through unmasked | S1 |
| Login failure | step-1 `Location` has `error=…&attempts=N&max_attempts=10` | surface the counter; never retry | S7 |
| Wrong region host | `403`, or valid-looking empty | §3.3 | S1 |

### 2.6 Cookie fallback (no password)
Log in at the web portal, lift the `apptoken` cookie, and take the numeric
`user_id` from any `/users/<id>/…` path in the app's traffic
`[S2]`, `[S9]`, `[S6 · README.md]`. Portal: `https://user.huami.com/privacy/index.html`
for `huami.com` accounts, `https://user.zepp.com/` for `zepp.com` ones. This is
the only path that works for SSO / Google / Apple / 2FA accounts — password login
cannot do any of those `[S1 · docs/01-login.md]`. For the RU account the cookie is
presumably issued for the `huami.com` portal `unverified` — confirm the cookie's
domain in the browser.

### 2.7 Login as curl

Phase 1 needs an AES body, so shell alone cannot do it — encrypt the urlencoded
form (§2.1) and post it as a raw body:

```bash
# body.aes = PKCS7-padded AES-128-CBC(urlencoded form), key/IV from §2.1
curl -si -X POST "https://api-user-us2.zepp.com/v2/registrations/tokens" \
  -H "x-hm-ekv: 1" -H "app_name: com.huami.midong" -H "appname: com.huami.midong" \
  -H "appplatform: android_phone" \
  -H "content-type: application/x-www-form-urlencoded; charset=UTF-8" \
  --data-binary @body.aes | grep -i '^location:'      # expect 303

# then, with ACCESS=<the access= value from that Location>
curl -s -X POST "https://api-mifit-us2.zepp.com/v2/client/login" \
  -H "app_name: com.xiaomi.hm.health" -H "appname: com.huami.webapp" \
  -H "origin: https://user.zepp.com" -H "referer: https://user.zepp.com/" \
  --data-urlencode "code=$ACCESS" --data "device_id=$(uuidgen)" \
  --data "device_model=android_phone" --data "app_version=9.12.5" \
  --data "app_name=com.xiaomi.hm.health" --data "country_code=US" \
  --data "grant_type=access_token" --data "third_name=huami" \
  --data "allow_registration=false" --data "lang=en" \
  --data "source=com.huami.watch.hmwatchmanager:9.12.5:151689"
```

Read `token_info.app_token` / `user_id` / `app_ttl` and `domains[]` from the JSON.

### 2.8 Curl shape used by every example below
```bash
TOKEN="<app_token>"; UID="7007417683"; HOST="api-mifit-ru.huami.com"
curl -s -H "apptoken: $TOKEN" -H "appname: com.huami.midong" \
  -H "appplatform: ios_phone" -H "v: 2.0" -H "vn: 10.2.5" \
  "https://$HOST/v1/data/band_data.json?userid=$UID&from_date=2026-11-01&to_date=2026-11-01&query_type=detail&byteLength=8&device_type=0&r=$(uuidgen)"
```

---

## 3. Regional hosts
### 3.1 Hosts seen in the wild
| Host | Region | Seen in | Notes |
|---|---|---|---|
| `api-mifit.zepp.com` | global default | S6, S7, S8 | no suffix; S8 uses it for `/users/{id}/devices` regardless of region |
| `api-mifit-us2.zepp.com` | US legacy | S1, S2, S8 | S3 records `api-mifit-us2.huami.com` as deprecated |
| `api-mifit-us3.zepp.com` | US current | S3, S7 | S7 resolved to it from the login response |
| `api-mifit-de2.zepp.com` | EU (`eu-central-1`) | S1, S2 | S1's validated EU host |
| `api-mifit-de.zepp.com` | EU | S3 | S3 flags regional guesses as needing re-verification |
| `api-mifit-cn.huami.com` | CN | S10 | vendor doc only |
| `api-mifit.huami.com` | global legacy | S2, S10 | older host family |
| `api-mifit-ru.huami.com` | **RU** | task brief only | matches the pre-`.zepp.com` naming; no repo here uses it |
| `api-user-us2.zepp.com`, `api-user.zepp.com` | login (phase 1) | S1/S8, S7 | login hosts are **not** regional |
| `api-mifit-us2.zepp.com`, `api-mifit.zepp.com` | login (phase 2) | S1/S8, S7 | phase 2 also works cross-region |

Synthesis (L4): `huami.com` is the older regional deployment, `*.zepp.com` the
newer CNAME layout. A RU account predating the migration plausibly still resolves
to `api-mifit-ru.huami.com`. Do not infer that other `-ru` hosts exist.

### 3.2 Discovery, in order of trust
1. **Read the login response.** `domains[]` = `[{host, cnames:[…]}]`; pick the
   entry whose `host` is `api-mifit.zepp.com`, take `cnames[0]`
   `[S7 · auth.py]`. The only method that does not guess.
2. **Read it off the phone.** Any captured request to `api-mifit-*` carries the
   host, the `apptoken` header, and the `user_id` in the path
   `[S3 · README.md]`, `[S6 · README.md]`. Needs a TLS proxy + trusted CA.
3. **Probe candidates** with `band_data` for yesterday; "non-empty, schema-valid
   JSON" is the signal. Sequentially, with delays — a wrong host is cheap, but a
   host sweep looks like abuse.

### 3.3 What a wrong host returns
| Symptom | Meaning | Src |
|---|---|---|
| `403` | host does not serve this account | S1 |
| `200`, empty `data`/`items` | host serves the API, not your shard | S1 |
| `200` + `application/octet-stream` | you sent `x-hm-ekv: 1` (§2.4), not a host problem | S2 |
| `404` on a path that works elsewhere | wrong host and/or path absent there | S7 |
| `500` `{"code":-50000}` | missing required headers (`appPlatform`, `appname`) more often than a bad host | S2 |
| login OK, every data call empty | you are on the login host, not the data host | S1 |

Phase 2 on the data host also works (`api-mifit.zepp.com/v2/client/login` in S7),
so one host variable for "phase 2 + data" is a reasonable design — but on this
account, verify phase 2 against `api-mifit-ru.huami.com` before assuming it is
served there `unverified`.

---

## 4. Endpoint catalogue
All paths relative to the regional data host; all `GET` unless marked; every
request needs `apptoken` + `appname` (§2.4).

### 4.1 Master table
| # | Method | Path | Required params | Returns | Notes | Src |
|---|---|---|---|---|---|---|
| 1 | POST | `/v2/registrations/tokens` | AES body (§2.1) | `303` + `Location` | phase 1, on `api-user-*` | S1,S8 |
| 2 | POST | `/v2/client/login` | `code`, `app_name`, `device_id` | `token_info`, `regist_info`, `domains` | phase 2, on `api-mifit-*` | S1,S7,S8 |
| 3 | POST | `/v1/client/logout` | `login_token`, `os_verison` (sic) | `{result:"ok"}` | invalidates `login_token` only | S8 |
| 4 | GET | `/v1/data/band_data.json` | `userid`, `from_date`, `to_date`, `query_type` | per day: `summary` (b64 JSON), `data_hr` (b64 bytes), `data` (b64 bytes) | the workhorse | S1,S2,S3 |
| 5 | GET | `/v2/users/me/events` | `eventType`, `subType`, `from`, `to` (ms) | `items[].value{}` | multi-purpose; presets §4.3 | S1,S2,S6 |
| 6 | GET | `/users/{id}/events` | `eventType`, `from`, `to` (ms), `userId` | `items[]` | **different scope**: stress/PAI/SpO2 | S1,S6,S7 |
| 7 | GET | `/users/{id}/events/dateString` | `eventType`, `subType`, ISO `from`/`to`, `timezone` | `items[]` | ISO window; ODI/OSA | S6,S7 |
| 8 | GET | `/v2/watch/users/{id}/WatchSportStatistics/{NAME}` | `startDay`, `endDay`, `limit`, `isReverse` | `items[]` with `dayId` | `NAME ∈ {SPORT_LOAD, VO2_MAX}`; without params → 500 | S1,S2,S7 |
| 9 | GET | `/v1/sport/run/history.json` | `userid`, `source` (literal) | `data.summary[]`, ~150 fields/row | returns **all** sports; paginate `data.next` | S1,S7 |
| 10 | GET | `/v1/sport/run/detail.json` | `trackid`, `source` | packed per-stream strings | GPS/HR/pace/altitude/laps | S1,S4,S7 |
| 11 | GET | `/users/{id}/heartRate` | `startTime`, `endTime` (s), `limit`, `type=2` | `items[]` per-minute HR | second HR source | S3,S6 |
| 12 | GET | `/users/{id}/members/-1/weightRecords` | `fromTime`, `toTime` (s), `limit` | `items[].summary{}` | weight/body comp | S1,S6,S7 |
| 13 | GET | `/huami.health.getUserInfo.json` | `userid` | `{code,data:{age,gender,height,weight…}}` | profile; token smoke test | S3,S6,S7 |
| 14 | GET | `/users/me/fileInfo/events` | `eventType=second_heart_rate`, `subType=real_data`, `from`, `to` | file index with `fileId` | file route unknown | S1,S6,S7 |
| 15 | GET | `/users/me/bloodPressure` | `days`, `sourceArrayStr`, `toDate` | BP records | manual/app entries | S6 |
| 16 | GET | `/v1/user/manualData.json` | `userid`, `type=sleep` | manual entries | app-logged sleep | S6 |
| 17 | GET | `/users/{id}/devices` | `enableMultiDevice=true`, `userid`, `appid` | devices incl. `additionalInfo.auth_key` | **never surface `auth_key`** | S7,S8 |

### 4.2 Per endpoint
**#4 `band_data`** — `query_type=detail` is a superset of `summary` (88 KB vs
27 KB on one day) `[S7]`. `byteLength` sets the `data` blob width (§6.3);
`device_type=0` and `android_phone` both accepted `[S2]`. One object per day.

```json
{"code":1,"message":"success","data":[{
 "uid":"<uid>","data_type":0,"date_time":"2026-11-01","source":1234567,
 "device_id":"<sn>","uuid":"<uuid>",
 "summary":"eyJ2IjoiNiIsImdvYWwiOjgwMDA,…",   // base64 → JSON (§6.1)
 "data_hr":"MnQ9ADIB…",                         // base64, 1 B/min (§6.2)
 "data":"AAAAAA…"}]}                            // base64, N B/min (§6.3)
```

Decoded `summary` from a real payload `[S1 · tests/fixtures/band_data_day.json]`:

```json
{"v":"6","goal":8000,"tz":"3600","algv":"2.13.14","sn":"<sn>","byteLength":8,
 "sync":1153958000,
 "stp":{"ttl":8272,"dis":3545,"cal":222,"wk":28,"rn":49,"runDist":956,"runCal":198,
        "stage":[],"stepStageSummary":[]},
 "slp":{"stage":[{"start":1440,"stop":1509,"mode":4}],"odd_stage":[],
        "spos":0,"spol":0,"spor":0,"spob":0,
        "st":1153958400,"ed":1153974900,"obt":10,"ebt":500,
        "dp":60,"lt":180,"wk":5,"wc":1,"dt":30,"rhr":59,"ss":89,"to":0,
        "supNap":true,"supRem":true,"is":30,"lb":10,
        "sleepScoreVersion":"1.0.2","sleepVersion":0,
        "sleepAlgoVersion":"4.0.17","napAlgoVersion":"4.0.17"},
 "hr":{"maxHr":{"hr":158,"ts":1770422906}}}
```

`S2` adds `slp.ps/pe`, `stp.stage[]` activity segments; `S7` adds
`stepStageSummary[]` and the `sleep*Version` block. Tolerate unknown keys.

**#5 `/v2/users/me/events`** — `reverse=true` newest first; `/me`, no user id in
the path.

> **Correction, verified live (see §A.4).** Do **not** send `from`/`to`. On the
> T-Rex 3 Pro account both `/users/{id}/events` and `/v2/users/me/events`
> return `{"items":[]}` as soon as a range is present, in ms or in seconds,
> whatever `limit` says. Without them the same query returns records
> immediately. Use `reverse=true` + `limit` and filter by `timestamp`
> client-side.

```json
{"items":[{"userId":"<uid>","eventType":"Charge","subType":"real_data",
 "timestamp":1153872000000,
 "value":{"startTime":1153872000000,"deviceId":"1440,app","deviceSN":"1440,",
  "deviceSource":"1440,2","deviceType":"1440,2","timeZone":"1440,Europe/Madrid",
  "samples":[{"s":0,"total":56,"mental":49.0456,"physical":55.4176,
              "status":0,"jsonExtra":"{}"}]}}]}
```

The `"1440,"` prefix on `deviceId`/`timeZone` is a **sample count**; the zone is
the text after the last comma `[S7]`. `[S1 · tests/fixtures/biocharge_day.json]`

**#6 `/users/{id}/events`** — same grammar plus `userId`, different scope: this is
where the stress series the app draws lives; `Charge/stress_data` from #5 does not
contain it `[S1 · docs/zepp-api.md]`.

```json
{"items":[{"userId":"<uid>","eventType":"all_day_stress","subType":"all_day_stress",
 "timestamp":1770357600000,"deviceType":"0","deviceSn":"<sn>",
 "avgStress":"43","maxStress":"67","minStress":"5",
 "relaxProportion":"30","normalProportion":"58","mediumProportion":"12","highProportion":"0",
 "data":"[{\"time\":1770357600000,\"value\":47},…]"}]}
```

Aggregates are **strings**; `data` is a **JSON string** of `[{time,value}]` with
absolute epoch ms (§6.6) `[S2]`.

**#7 `events/dateString`** — ISO window plus explicit zone; used for
`blood_oxygen/odi` and `osa_event` `[S6]`, `[S7]`.

```json
{"items":[{"timestamp":1770357600000,"odi":3.21,"odiNum":27,
           "cost":30240,"score":88,"valid":1}]}
```

Free check: `odi ≈ odiNum / (cost/3600)` (desaturations per hour), and `cost` is
that night's measured seconds `[S7 · capability-matrix.md]`.

**#8 `WatchSportStatistics`**

```json
{"items":[{"dayId":"2026-11-01","generatedTime":1770336000,"device_source":1234567,
 "currnetDayTrainLoad":56,"wtlSum":379,"wtlSumOptimalMin":330,
 "wtlSumOptimalMax":735,"wtlSumOverreaching":864,"updateTime":1770437852000}]}
```

`dayId` may be `"YYYY-MM-DD"` or an 8-digit int `YYYYMMDD`
`[S1 · parsers.py]`. `currnetDayTrainLoad` (sic) is the **day's** load; `wtlSum`
is a **7-day sum** — never substitute one for the other. Discriminator worth
reusing: on this route `400` = unknown statistic name, `500` = name recognised
but the call failed (a fabricated name gives 400) `[S7]`.

**#9 `history`** — `source` is a fixed literal, **not a filter**: every workout
comes back regardless of sport, and other path slugs (`walking`, `cycling`,
`swimming`, `strength`) return **404** `[S1]`, `[S7]`. Pagination: `data.next`
(`-1` when exhausted) is the newest evidence; the app form is
`startTrackId`/`stopTrackId` (unix s). S7 walks `trackid=<data.next>` and guards
against a repeating page. Implement both.

```json
{"code":1,"message":"success","data":{"next":-1,"summary":[{
 "trackid":"1786761306","source":"run.12345678.huami.com",
 "end_time":"1786764309","run_time":"2730",
 "type":52,"sport_title":"","auto_recognition":false,
 "avg_heart_rate":"115.0","max_heart_rate":"158","min_heart_rate":"72",
 "calorie":"-1.0","exercise_load":"34","te":"32",
 "heart_range":"0,98;242,118;20,137",
 "dis":"0.0","total_step":0,"max_altitude":-20000,"min_altitude":-20000,
 "parent_trackid":-1,"child_list":"",
 "add_info":"{\"upload_gps_permission\":\"allow\"}",
 "runningProgram":0,"dailyPlanFinished":false,
 "syncedTimezone":"Asia/Kolkata","bind_device":"0:<MODEL>:<SN>:0.0.0.0"}]}}
```

Types are mixed **within one row**: `trackid`, `end_time`, `run_time`,
`avg_heart_rate`, `calorie`, `te` are strings; `type`, `auto_recognition`,
`max_altitude` are numbers; `add_info`, `pb`, `strength_training_group` are
JSON-encoded strings `[S1]`, `[S7]`. `[S1 · fixtures/sport_history.json]`

**#10 `detail`** — every value is a packed string, and which streams exist
depends on the sport. `trackid` must come from the index row together with its
`source`.

```json
{"code":1,"data":{"heart_rate":"22,117;1,-1;1,2;…",
 "longitude_latitude":"1854240229,7379252091;151,39;…",
 "altitude":"12345;0;…","air_pressure_altitude":"1,101325;1,101300;…",
 "pace":"322.0;…","speed":"1,2.83;…","distance":"1,0;1,3;…",
 "time":"1;1;1;…","gait":"1,3,74,153;…","pause":"…","lap":"…","kilo_pace":"…"}}
```

Decoders in §6.7 `[S7]`, `[S4 · zepp_sports.py]`.

**#11–#17 — compact**

| # | Shape | Caveats |
|---|---|---|
| 11 | `{"items":[{"time":<ms?>,"value":62}]}` | item shape never captured in a fixture; S3 accepts `{time,value}` **or** `{timestamp,bpm}` and drops `bpm<=0`; unit ambiguous — detect by magnitude `unverified` |
| 12 | `{"items":[{"generatedTime":<s>,"weightType":1,"summary":{"weight":70.0,"height":175,"age":21,"bmi":27.8,"encryptImpedance":"0","bodyBalanceScore":93,"source":2}}]}` | `encryptImpedance` is the plain impedance as a string, not a cipher; `bmi` can contradict `weight/height` in the record itself — compute and flag `[S7 · body.py]` |
| 13 | `{"code":1,"data":{"age":41,"gender":1,"height":178,"weight":74,"birthday":"1985-…"}}` | cheap liveness check (`code==1`) `[S3]` |
| 14 | file index with `fileId` → zipped `SEC_HR` blob | serving route never found `[S1]`, `[S7]` |
| 15 | BP records; params `days`, `toDate`, `sourceArrayStr` (`com.huami.midong.associated,com.huami.midong`) | manual/app entries, not the watch `[S6]`; shape `unverified` |
| 16 | manual entries; `type=sleep` is the only documented value | shape `unverified` `[S6]` |
| 17 | devices with `macAddress`, `activeStatus`, `additionalInfo` (JSON string) containing `auth_key` | BLE pairing secret: allowlist output, never dump `[S7]`, `[S8]` |

Body-composition fields beyond weight/height/bmi (`fatRate`, `bodyWaterRate`,
`muscleRate`, `boneMass`, `metabolism`, `proteinRatio`, `visceralFat`,
`bodyScore`, `impedance`, …) come from **one external capture**
(`AlexxIT/SmartScaleConnect`), not a verified account; `muscleRate`'s unit is
unknown even to that client's author `[S7]`.

### 4.3 Event presets
`eventType`/`subType` are exact literals, and there is no discovery endpoint
(§10.2).

| Endpoint | eventType | subType | Carries | Status | Src |
|---|---|---|---|---|---|
| #5 | `Charge` | `real_data` | BioCharge `samples[{s,total,mental,physical,status}]` | L1 | S1,S4,S7 |
| #5 | `readiness` | `watch_score` | `rdnsScore`, `sleepHRV`, `sleepRHR`, `skinTempCalibrated`, `ahiScore`, `afibScore` | L1 | S1,S4,S7 |
| #5 | `HRVRMSSD` | `real_data` | per-minute RMSSD (`hrv`) | L2 | S1,S4,S7 |
| #5 | `hrv_sdnn` | `real_data` | SDNN spots (`sdnn`, `u`) | L4 unit | S1,S4,S7 |
| #5 | `RespiratoryRate` | `real_data` | 1440-byte b64, 1 B/min | L2 | S1,S4,S7 |
| #5 | `DailyHealth` | `summary` | daily totals + goals | L2 | S1,S2,S6,S7 |
| #5 | `LactateThreshold` | `summary` | `samples[{lactateThresholdHr,lactateThresholdPace,dateString}]` | L1 | S7 |
| #5 | `exertion` | `algo_result` | ATL/CTL/TSB, `exercisePlan`, `activities[]` | L3 | S2 |
| #5 | `phn` | `daily_analysis` | `result{trimp,atl,ctl,tsb}` | L3 | S2 |
| #5 | `sport` | `recovery_hr` | post-workout HR recovery | L4 | S2,S7 |
| #5 | `blood_pressure` | `real_data` / `manually_add_data` | systolic/diastolic | L4 | S2,S6 |
| #5 | `Emotion` | `real_data` | empty everywhere tried | L5 | S1,S6 |
| #5 | `Charge` | `stress_data` | `stressInfo`: dense model, **not** the app curve | L5 rejected | S1,S6,S7 |
| #5 | `Charge` | `insight_data` | named by one external client | L4 | S7 |
| #5 | `EmotionBaseline` / `skinTemp` | `real_data` | baseline values / skin temp | L4 | S2,S7 |
| #6 | `all_day_stress` | none or `all_day_stress` | sparse `{time,value}` + string aggregates | L1 | S1,S2,S6,S7 |
| #6 | `single_stress` | none | `{timestamp,value}` spots | L2 | S1,S6,S7 |
| #6 | `PaiHealthInfo` | none | `dailyPai`, `totalPai`, zone PAI/minutes/limits, `restHr`, `maxHr` | L2 | S4,S6,S7 |
| #6 | `blood_oxygen` | `click` | spot SpO2 in JSON-string `extra` | L2 | S3,S4,S6,S7 |
| #6 | `health_data` | `blood_pressure` | manual BP | L4 | S6,S7 |
| #7 | `blood_oxygen` | `odi` / `osa_event` | ODI + breathing score; OSA empty | L2 / L5 | S6,S7 |

---

## 5. Per-metric resolution
"Decode" points at the section explaining the wire format.

| Metric | Endpoint(s) | Resolution | Decode | Status |
|---|---|---|---|---|
| Steps (daily) | #4 `stp.ttl`; #5 `DailyHealth.totalSteps` | 1/day | §6.1 | L1 (equal on shared days) |
| Steps (intraday) | #4 `data` byte 2 of each 3-byte minute | 1/min | §6.3 | L2 (sums to `ttl`, 8/8 d) |
| Steps (segments) | #4 `stp.stage[]` (`start`,`stop`,`mode`,`step`,`dis`,`cal`) | per segment, stop-inclusive | §6.1 | L2 |
| Distance | #4 `stp.dis`; #5 `totalRunningDistance` (**on-foot workouts only**) | 1/day | — | L1 / L2 |
| Calories (active) | #4 `stp.cal`; #5 `totalCalories` | 1/day | — | L1; `cal` is active, not total |
| Active minutes | #5 `totalBurningDuration` + `burningDurationGoal` | 1/day | — | L2 |
| Heart rate | #4 `data_hr`; #11 `/heartRate` | 1/min | §6.2 | L1 / L4 shape for #11 |
| Resting HR | #4 `slp.rhr`; #5 `readiness.sleepRHR` | 1/night | — | L1 (equal 6/6) |
| Max HR (day) | #4 `summary.hr.maxHr.{hr,ts}` | 1/day | — | L2 |
| HR zones | #9 `heart_range` | per workout | §6.7 | L2 |
| Sleep score | #4 `slp.ss`; `readiness.sleepScore` | 1/night | — | L1 |
| Sleep stages | #4 `slp.stage[]` | per segment, stop-inclusive, minutes from a reference midnight | §9 | L2 |
| Sleep totals | #4 `slp.lt/dp/dt/wk` | minutes/night | §9.1 | L2 |
| Bed window | #4 `slp.st/ed` (unix s), `obt/ebt/is/lb` (min) | 1/night | §9.6 | L2 |
| Naps | #4 `slp.odd_stage[]`, group by contiguity | per segment, no score | §9.4 | L2 shape |
| Sleep breathing (ODI) | #7 `blood_oxygen/odi` | 1/night | — | L2 |
| SpO2 spot | #6 `blood_oxygen/click` → `extra.spo2` | event, irregular | §6.5 | L2 |
| SpO2 continuous | **none found** | — | — | L5 |
| Stress | #6 `all_day_stress` → `data` | sparse, explicit gaps | §6.6 | L1 (18/18 rows vs app) |
| Stress single | #6 `single_stress` | event | — | L2 |
| HRV RMSSD | #5 `HRVRMSSD`; curated in `readiness.sleepHRV` | ~1/min in stretches | §6.4 | L2 |
| HRV SDNN | #5 `hrv_sdnn` | few/day | §6.4 | L4 unit |
| PAI | #6 `PaiHealthInfo` | 1/day, multiple uploads → keep latest | — | L2 (`totalPai`=Σ`dailyPai`, 75/day cap) |
| Readiness | #5 `readiness/watch_score` | 1/day, revised intraday → keep max `item.timestamp` | §7.4 | L1 |
| BioCharge | #5 `Charge/real_data` | 1/min | §6.4 | L1 `total`; components L4 |
| Respiratory rate | #5 `RespiratoryRate` | 1/min, mostly zero (asleep) | §6.5 | L2 |
| Training load (day) | #8 `currnetDayTrainLoad` | 1/day | — | L2 |
| Training load (7 d) | #8 `wtlSum` + 3 ranges | 1/day rolling | — | L2 |
| ATL/CTL/TSB | #5 `exertion/algo_result` | 1/day | — | L3 |
| TRIMP | #5 `phn/daily_analysis` | 1/day | — | L3 |
| VO2max | #8 `VO2_MAX`; also per-workout `VO2_max` in #9 | 1/day? | — | **L5** series shape never captured; per-workout copy L2 |
| Lactate threshold | #5 `LactateThreshold/summary` | per dated estimate | — | L1 |
| Skin temperature | #5 `readiness.skinTempCalibrated` | 1/night | §8.3 | L4 unit |
| Weight / body comp | #12 `weightRecords` | event | §4.2 | L2 weight/height/bmi, L3 rest |
| Workouts | #9 (+#10 streams) | event | §6.7 | L1 |
| Manual BP | #15, #6 `health_data/blood_pressure` | event | — | L4 |
| Per-second HR | #14 index only | 1/s, file-based | — | L5 |

Displayed by the app but **not exposed as a series**: continuous SpO2, ECG/AFib
(only `afibScore`, 255 everywhere), standing/sedentary time, sleep regularity,
raw IMU.

---

## 6. Binary and text encodings
### 6.0 Encoding index — which field is which

| Field | Encoding | Endpoint |
|---|---|---|
| `day.summary` | base64 → **JSON text** | #4 |
| `day.data_hr` | base64 → **raw bytes**, 1 B/min | #4 |
| `day.data` | base64 → **raw bytes**, N B/min | #4 |
| `value.measurements` | base64 → **raw bytes**, 1440/day | #5 `RespiratoryRate` |
| `item.data` (stress) | **JSON string** → array of `{time,value}` | #6 |
| `item.extra` (SpO2) | **JSON string** → object | #6 |
| `history.add_info`, `pb`, `strength_training_group`, `child_list` | **JSON string** | #9 |
| `detail.*` streams | `token;token;…` packed text, **not JSON** | #10 |
| `fileInfo.fileId` | id → zipped binary, fetch route unknown | #14 |
| `trackid`, `end_time`, `run_time`, HR fields, `te`, `calorie`, stress aggregates | **numbers as strings** (§8.2) | #9, #6 |

### 6.1 `summary` — base64 JSON
`json.loads(base64.b64decode(day["summary"]))`. Tolerate unknown keys —
`spos`, `spol`, `stepStageSummary`, `sleepAlgoVersion` are all optional across
firmwares.

`stp.stage[]` segments `{start, stop, mode, step, dis, cal}` use
**minute-of-local-day** indices with `stop` inclusive — proven because the
per-minute step bytes reproduce every stage's `step` total `[S7 · decode.py]`.
Mode names are contested: S7 reads `3`=walking, `4`=running (cadence evidence
~90–100 vs ~150 spm); S2's table says `1` slow walking, `3` fast walking,
`7` running, `76` light activity. Keep the raw code; treat names as L4.

### 6.2 `data_hr` — 1 byte per minute
```python
raw = base64.b64decode(day["data_hr"])   # 1440 bytes for a full day
bpm = raw[i]                             # i = minutes since local midnight
```

- `0`, `254` are "no reading"; `255` also appears `[S1]`, `[S7 · codes.py]`.
- Plausible range 30–220 (S1 drops outside it); S2 accepts `0 < b < 254`. Drop
  sentinels first, then range-check and log rejects.
- Never interpolate across sentinels — a 254 rendered as bpm is a spike that
  never happened.
- Length 1440 on a normal day. On DST days whether it becomes 1380/1500 is
  `unverified`: assert `len(raw)` and treat other values as a distinct layout.

### 6.3 `data` — N bytes per minute, only byte 2 decoded
`byteLength` sets bytes per minute: `3` and `8` both observed (4320 vs 11520
bytes/day) `[S2]`, `[S1 · fixtures/band_data_day.json]` (`byteLength: 8`,
11520-byte blob), `[S6]` (default `byte_length=8`).

The **3-byte** layout, decoded as far as it goes:

| Byte | Meaning | Evidence |
|---|---|---|
| 0 | activity kind (raw) | other clients read its low nibble as a kind code (L3) |
| 1 | intensity (raw) | not decoded |
| 2 | **steps this minute** | sums to `stp.ttl` 8/8 days; reproduces 97/97 step-stage totals `[S7]` |

`unverified`: whether bytes 0–2 keep those meanings at `byteLength=8` (L4 —
plausible, unchecked); what bytes 3–7 carry; multi-byte endianness. Five bytes
remain after the triple, so the hypotheses are (a) 32-bit distance in metres +
1 tag byte, (b) 16-bit distance + 16-bit calories + 1 pad, (c) a wider repeat of
the triple with padding. None is verified and no source decoded them. Verify with
`sum(byte[2::8]) == stp.ttl` after requesting the same day at 3 and 8. S1 never
parses `data` at all (it only records `len()`), which is the honest baseline.

### 6.4 `Charge` and HRV sample containers
```json
"value":{"startTime":1153872000000,
         "samples":[{"s":0,"total":56,"mental":49.0456,"physical":55.4176,
                     "status":0,"jsonExtra":"{}"}]}
```

`ts = (startTime + s)/1000`; both absolute UTC, `s` in **ms**
`[S1 · parsers.py]`. `total == 255` = "not computed", in short mid-day runs, not
only at the end of the synced window; `mental`/`physical` stay valid there, so
null `total` and keep the sample. `status` is always 0 and is useless as a
filter `[S1]`. `total ≈ mean(mental, physical)` within 1
`[S7 · capability-matrix.md]`.

HRV uses the same container: `{s, hrv}` for RMSSD, `{s, sdnn, u}` for SDNN
`[S7 · events.py]`, `[S4]`. SDNN's unit is inferred from the name and a 20–100
range — nothing cross-checks it.

### 6.5 Respiratory rate and SpO2
Respiratory: `value.measurements` is base64 of **1440 bytes**, 1/min, `0` = no
reading (950–1339 zeros/day is normal — measured mostly asleep); real values
14–21 `[S1 · parsers.py]`. `value.timeZone` = `[{period:[start_min,end_min],
offset:seconds}]`; the periods bound the non-zero bytes exactly (6/6 days), so it
is a rendering hint, not an anchor.

SpO2 spot check (`blood_oxygen/click`) hides the reading in a **JSON string**:

```json
{"timestamp":1770357600000,
 "extra":"{\"spo2\":99,\"isAuto\":true,\"spo2History\":[…60 values…]}"}
```

Take `extra.spo2`, `extra.isAuto`, `extra.timestamp`; ignore `spo2History`
(60-slot rolling window, mostly zeros) `[S3]`, `[S7]`. Drop `<=0`, `<50`, `>100`.

### 6.6 Stress — JSON inside a JSON string
`item.data` is a **string** holding `[{"time":<epoch ms>,"value":<0-100>}]`.
Samples are **sparse and explicit**: a missing minute is a real gap and the app
draws it as one `[S1]`, verified against the app's own screen (18/18 rows across
two stretches). S2 describes "5-minute intervals" — that is their capture's
rendering cadence, not a guarantee; S1's ground truth contains consecutive
one-minute samples. Trust timestamps, never an assumed step.

Day aggregates (`avgStress`, `maxStress`, `minStress`, `*Proportion`) are
**strings** and recompute exactly from the item's own samples `[S7]`. Zone
boundaries disagree: S2 uses 1–25/26–50/51–75/76–100; S7 recomputes with
<40/40–59/60–79/≥80 and matches the payload's own proportions 6/6 days — S7 wins,
S2's are L3.

### 6.7 Workout detail — ~70 packed strings, delta-encoded
Each value is `token;token;…`, one token per sample with comma-separated columns
`[S7 · docs/api-findings.md]`; decoders `[S4 · zepp_sports.py]`.

| Stream | Format | Decode |
|---|---|---|
| `heart_rate` | `dt,dhr;…` both **deltas** | `t += dt` (blank→1), `hr += dhr`; `dhr` may be negative |
| `time` | `dt;…` | cumulative seconds from start |
| `longitude_latitude` | `lat,lon;dlat,dlon;…` **lat first**, per-axis delta, ×1e-8 | accumulate per axis, /1e8 |
| `altitude` | absolute ×1e-2; sentinel `-2000000` | /100 → m; carry last valid over sentinel |
| `air_pressure_altitude` | `dt,pressure_pa;…` absolute Pa | `44330*(1-(P/101325)^(1/5.255))` |
| `pace` | absolute s/m | as-is |
| `speed` | `dt,speed;…` absolute m/s | cumulative `t` |
| `distance` | `dt,dd;…` delta | `cumsum(dd)` m |
| `gait` | `dt,dsteps,stride_cm,cadence;…` | cumsum steps; stride/cadence absolute (cadence may be 0) |
| `kilo_pace` | positional split rows | **column 2 is a geohash — strip it** `[S7]` |
| `lap` | positional, sport-dependent | swim cols 1/13/14 = duration/strokes/SWOLF; strength 1/4/5/21/22/28 `[S7]` |
| `pause` | positional | reconciles with `pauseTimeWithMillis` ±1.5 s `[S7]` |

Nested JSON strings needing a second `json.loads`: `add_info`
(`upload_gps_permission`, `adjust_dem_altitude`), `pb`, `strength_training_group`
(`[{"count":182},{"count":86}]` = reps per set) `[S1]`, `[S7]`.

`te` arrives ×10 everywhere: `"32"` = 3.2, `anaerobic_te: 1` = 0.1
`[S1 · parsers.py]`, `[S7 · codes.py]`.

`heart_range` = `seconds,upper_bpm;…`, each slice counting time between the
previous upper bound and this one; the seconds sum to session duration (3909 vs
`run_time` 3921, 0.3 % apart) — **not cumulative** `[S1]`, `[S7 · decode.py]`.

---

## 7. Time anchoring
### 7.1 Table
| Payload | Anchor | Unit | Lvl | Src |
|---|---|---|---|---|
| `band_data.date_time` | date string naming the day covered | date | L1 | S1,S2 |
| `band_data.data_hr[i]` | i=0 → **local** midnight of `date_time` | 1 min/byte | L1 (vs `slp.st`) | S1 |
| `summary.stp.stage[].start/stop` | minute of **local** day, stop inclusive | min | L2 | S7 |
| `summary.slp.stage[].start/stop` | minutes from a **local** midnight — which one is disputed (§9.2) | min, inclusive | L2 | S1,S7 |
| `summary.slp.st/ed` | absolute unix **seconds** — the only trustworthy sleep anchor | s | L1 | S1,S4,S7 |
| `summary.slp.obt/ebt/is/lb` | minutes, same coordinate as `stage[]` | min | L2 | S7 |
| `summary.hr.maxHr.ts` | absolute unix seconds | s | L2 | S2 |
| `RespiratoryRate.measurements[i]` | i=0 → `item.timestamp` = **UTC** midnight | 1 min/byte | L1 (2 checks) | S1,S7 |
| `RespiratoryRate.value.timeZone[]` | metadata (offset ruling a stretch) | s | L2 | S1 |
| `Charge.value.startTime` + `s` | absolute **UTC ms** + ms offset | ms | L1 | S1,S7 |
| `readiness.value.timestamp` | **local** midnight of day scored | ms | L1 (9/9) | S1 |
| `readiness.item.timestamp` | when computed (≈ wake; can cross midnight) | ms | L1 | S1 |
| `all_day_stress.item.timestamp` | **UTC** midnight (multiple of 86400000) | ms | L1 (7/7) | S1 |
| `all_day_stress.data[].time` | absolute ms per sample | ms | L1 | S1,S2 |
| `DailyHealth.samples[].dateString` | local day **resolved by server** | date | L2 | S1 |
| `DailyHealth.item.timestamp` | ms; day comes from `dateString` | ms | L2 | S1 |
| `hrv_*`, `Charge` sample `s` | offset from `value.startTime` | ms | L2 | S7 |
| `WatchSportStatistics.dayId` | `YYYY-MM-DD` or int `YYYYMMDD` | date | L2 | S1 |
| `history.trackid` | workout **start**, unix s (string) | s | L1 | S1,S7 |
| `history.end_time` | unix s (string) | s | L1 | S7 |
| `detail.*` time streams | seconds since session start, delta-encoded | s | L2 | S4,S7 |
| `weightRecords.generatedTime` | unix s | s | L2 | S7 |
| `heartRate.items[].time` | ms (modern) but never captured — detect by magnitude | ms? | L4 | S3 |
| `blood_oxygen/odi`, `PaiHealthInfo` | local midnight (v1 has no tz field) | ms | L2 | S7 |

### 7.2 The DST trap
`data_hr` and `slp.stage[]` are **minute offsets from a local midnight**. Never
convert with a fixed offset:

```python
# WRONG on 2026-03-29 / 2026-10-25 (EU), and every other DST boundary
ts = midnight_with_fixed_offset + minute * 60
# RIGHT
from zoneinfo import ZoneInfo
midnight = datetime(day.year, day.month, day.day, tzinfo=ZoneInfo("Europe/Bratislava"))
ts = int((midnight + timedelta(minutes=minute)).timestamp())
```

A 23-hour day means index 1439 is 22:59 local; a 25-hour day means 1440 bytes
cannot describe the day. Adding minutes to a fixed offset shifts every sample by
an hour after the transition and silently corrupts the whole day
`[S1 · docs/zepp-api.md]`.

Second-order trap: the watch's `tz` may change while travelling, and a payload's
`tz` describes the moment of sync, not the day. Anchor on the **configured**
zone (the one that defines the user's days), then reconcile: if the payload's
offset differs a lot for a day, the watch was elsewhere — but the Home Assistant
day is still the configured one.

### 7.3 Fields that lie about the timezone
| Field | Claims | Actually | Src |
|---|---|---|---|
| `band_data.summary.tz` | device offset in seconds (string) | frequently stale: `"3600"` in mid-summer Madrid where `7200` was right | S1 |
| `RespiratoryRate.value.timeZone` | `[{period,offset}]` | metadata for measured stretches; also said `3600` in summer. Using it as an anchor shifts the series ~1 h | S1 |
| `DailyHealth.value.timeZone` | a zone | server default `"1,Asia/Shanghai"` | S1 |
| `Charge.value.timeZone` | a zone | `"1440,Europe/Madrid"` — **count-prefixed**; zone after the last comma | S1,S7 |
| `readiness.value.timezoneId` | IANA zone | real, but spelled `Asia/Calcutta` while the same watch's workouts say `Asia/Kolkata`; a tz db without the alias falls back to UTC and moves every morning score to the previous day | S7 |
| v1 event items (`all_day_stress`, `PaiHealthInfo`, ODI) | nothing | **no timezone field at all**; timestamps are local midnight, zone inferred from request/`ZEPP_TIMEZONE` | S7 |
| `timezone:` request header | your zone | advisory; the server does not always use it | S1,S6 |

Rule: **never derive an instant from a timezone field.** Use an absolute epoch
when the payload has one, and *your* configured zone when it has only minute
offsets.

### 7.4 Readiness / stress / respiratory windows
- Readiness: day = `value.timestamp` (local midnight); `item.timestamp` = when
  computed (may be the next day). Zepp re-sends a day with updated scores (2
  revisions in 2/9 events): keep the **highest `item.timestamp` per day**
  `[S1]`, `[S7]`.
- Stress and respiratory are bucketed by **UTC** day, so a local-day window
  leaves the edge bucket half-filled. Back the start up to the containing UTC
  midnight and filter samples to the local day yourself:
  `from_ms -= from_ms % 86_400_000` `[S1 · run.py]`. Label an `all_day_stress`
  item with its **UTC** date — at any offset in −12 h…+12 h that is the local
  day owning most of the bucket (west of UTC the local date of `timestamp` is the
  previous day).

---

## 8. Sentinels and null conventions
### 8.1 Families
| Family | Sentinels | Where | Src |
|---|---|---|---|
| HR bytes | `0`, `254`, `255` | `data_hr` | S1,S2,S7 |
| Workout numeric, generic | `-1` | any numeric field | S1,S7 |
| Altitude | `-20000` (`-1`), detail `-2000000` at cm scale | `max_altitude`, `altitude` | S1,S4,S7 |
| Angle/bearing | `-361` | workout rows | S1,S7 |
| Elevation gain/loss | `-100`, `-1` | `altitude_ascend`, `climb_*` | S7 |
| Temperature | `-274`, `-273`, `-1` | `temperature` | S7 |
| Cadence/frequency | `-60`, `0`, `-1` | `avg_frequency`; `avg_cadence` is dead (always 0) | S7 |
| Pace | `0`, `-1` | `min_pace`, `max_pace` | S7 |
| Ratios | `0`, `-1` | flight/forefoot ratio | S7 |
| Percentage (SpO2 on rows) | `0`, `-1` | `percentage` | S7 |
| `rtpc` | `21.0`, `-1` | unconfirmed meaning | S7 |
| Readiness / PAI / BioCharge | `255` | `rdnsScore`, `sleepHRV`, `sleepRHR`, `phy`, `ment`, `afibScore`, `total` | S1,S4,S7 |
| Skin-temp baseline | `32767` (uncalibrated), `-10` | `skinTempBaseLine` | S7,S6 |
| Respiratory | `0`, skip `>=255` | `measurements` bytes | S1,S7 |
| SpO2 spot | `0`, `-1`; drop `<50`, `>100` | `extra.spo2` | S3,S7 |
| Stress | `single_stress` drops `value<=0`; `all_day_stress` 0–100 where 0 is a real low | series | S1,S7 |
| Weight/body comp | `0` = not measured | any scale field | S7 |
| Sleep `rhr` | `0` = no data | `slp.rhr` | S1 |
| Empty sleep | `st==ed`, `stage==[]`, `ss==0`, `rhr==0` | whole `slp` | S1 |

Build a **per-field** sentinel set, never one global set: `-1` is generic, but
`-20000` in an altitude field is not an altitude and `-361` in a bearing is not
a bearing. Null, never clamp.

### 8.2 Numbers that arrive as strings
| Field | Type | Example |
|---|---|---|
| `history.trackid`, `end_time`, `run_time` | string | `"1786761306"` |
| `history.avg_heart_rate`, `max/min_heart_rate`, `calorie`, `te`, `exercise_load`, `dis` | string | `"115.0"`, `"-1.0"` |
| `history.type`, `auto_recognition`, `max_altitude` | number/bool | `52`, `false` |
| `history.add_info`, `pb`, `strength_training_group`, `child_list` | JSON-string | `"[{\"count\":182}]"` |
| `all_day_stress.avgStress/maxStress/minStress/*Proportion` | string | `"43"` |
| `all_day_stress.data`, `blood_oxygen.extra` | JSON-string | `"[{\"time\":…}]"`, `"{\"spo2\":99}"` |
| `WatchSportStatistics.dayId` | string **or** int | `"2026-11-01"` / `20261101` |
| `weightRecords.summary.encryptImpedance` | string of an int | `"451"` |
| `summary.tz`, `Charge.value.timeZone` | string, sometimes `"<count>,<zone>"` | `"3600"`, `"1440,Europe/Madrid"` |

Lexicographic sorting of string `trackid` only works today because every unix
timestamp has ten digits. Coerce.

### 8.3 Skin temperature is a delta
`skinTempCalibrated` is an int; read as **hundredths of a °C of deviation from
the personal baseline** it spans −0.54…+0.82 °C on one account, which is
plausible `[S1]`. The unit is an **inference** — S7 exposes the same field as
`skin_temperature_offset_raw_unverified` instead of converting `[S7]`. Never ship
it as an absolute temperature.

---

## 9. Sleep semantics
### 9.1 What `slp` is
One object per `band_data` day, base64-JSON: one main night plus optional naps.
`st`, `ed` (unix s), `dp` deep, `lt` light, `dt` REM, `wk` awake minutes, `wc`
awake count, `ss` score, `rhr`, `is` latency, `obt`/`ebt` bed times, `stage[]`,
`odd_stage[]`, `supRem`/`supNap`, `sleepAlgoVersion`.

Verified identity, 7/7 nights `[S7 · capability-matrix.md]`:

```
lt + dp + dt + wk == (ed - st) / 60
st == obt + is      ebt == ed + lb
```

`dt` **is REM**. Two confirmations: S7's identity, and S1's fixture where the
mode-8 total (30) equals `dt` (30) and `lt+dp+wk+dt` equals `(ed−st)/60` (275).
S1 derives REM by summing mode-8 segments instead of reading `dt` — same answer,
more work. Reporting `dp+lt` as "total sleep" drops REM entirely (73/53/96 min on
three verified nights) `[S7 · codes.py]`.

### 9.2 Which date a night belongs to — the disputed part
| Claim | Repo | Evidence |
|---|---|---|
| `date_time` is the **wake** date; `stage[]` counts from the midnight **before** it | S1 `docs/zepp-api.md`, `parsers.py` | validated against `st`/`ed` live |
| `date_time` is the date you **fell asleep**; fetch day−1 too | S2, S4 `zepp_sleep.py`, S6 | prose only, no arithmetic shown |
| `stage[]` is anchored at local midnight of `date_time` itself | S2 sample | arithmetic below, exact to the second |

What the payloads actually show:

- S2's sample (`date_time 2026-02-06`, `tz "-21600"`, `stage[0].start 1471`,
  `st 1770445860`) satisfies `st == local_midnight(date_time, -06:00) + 1471 min`
  **exactly**. So the anchor is local midnight of `date_time`'s date — but that
  sample's `st`/`ed` fall on the *following* local date, contradicting both
  "start date" and "wake date" as labels. Its values were shifted when
  sanitised: use it for the *identity*, not the labels.
- S1's committed fixture is synthetic too (`sanitize_fixtures.py`:
  `st = anchor + 1440*60`, "ancla simple, no tz-aware") and it also anchors at
  `date_time` midnight. It cannot settle the question either.
- S1's parser is the only one claiming a live check against `st`/`ed`, and it
  uses midnight of `date_time − 1`.

**Do not pick a side — derive the anchor:**

```python
mid = local_midnight(date_time, tz)
ref = min([mid - timedelta(days=1), mid],
          key=lambda r: abs((r + timedelta(minutes=stage[0]["start"])).timestamp() - slp["st"]))
assert abs((ref + timedelta(minutes=stage[0]["start"])).timestamp() - slp["st"]) < 60
```

If neither candidate fits within 60 s, `stage[]` uses a coordinate you do not
understand: keep `st`/`ed`/`dp`/`lt`/`dt`/`wk` (absolute or plain minutes) and
drop the hypnogram rather than writing a night with wrong times.

To **find** a night, request `date_time` over `day−1 … day+1` and dedupe by
`(st, ed)` — two extra days are cheaper than a wrong date. S2's `get_sleep`
does exactly this (fetch `day-1` and `day`, pick the record whose `ed` matches).

### 9.3 Stages: modes and inclusive stops
`4` light, `5` deep, `7` awake, `8` REM — all L1 across sources.
`stop` is **inclusive**: `duration = stop − start + 1`. Only with `+1` do the
per-mode sums reproduce `lt`, `dp`, `wk` and `dt` exactly `[S1]`, `[S4]`, `[S7]`.
S2's `get_sleep` computes `stop − start` and is 1 minute short per segment — do
not copy. Offsets `> 1440` roll into the next day; with the right reference
midnight that falls out of the arithmetic, so never `offset % 1440`.

### 9.4 `odd_stage` — naps
Present on 22 of 39 days on S1's account, not a rare case. Same shape/anchor/
inclusive stop as `stage[]`, but a **flat list scattered across the day**:

- Segments of one nap are **adjacent** (`start == previous.stop + 1`).
- Different naps are separated by real gaps — ≥85 min observed.
- **Do not split on `mode == 7`**: one nap contained an 11-minute awake segment
  in the middle; splitting there turns one nap into three.
- Group by contiguity (S1 uses a 5-minute threshold), one session per group.
- A nap has no `ss`, `rhr`, `wc`, `dp`, `lt` — derive stage minutes by summing.

S4 parses `odd_stage` into a flat list without grouping — fine for display, wrong
for "how many naps". S2 records the edge case where the main `stage[]` is empty
but `odd_stage` is populated (overnight sleep filed there, `ss/dp/lt/rhr`
zeroed): detect `stage==[] and odd_stage!=[]` and treat the largest contiguous
group as the night, `unverified` for this watch.

### 9.5 Empty `slp` is not a night
A day may carry `slp` with `st == ed`, no `stage[]`, `ss` 0, `rhr` 0 — seen on a
day fetched before the night uploaded. Stored naively it becomes a 0-minute night
with a resting HR of 0 `[S1]`. Rules: no `slp` or `ed <= st` → no night;
`rhr == 0` → `None`; a night with duration but no stages **is** kept; a day with
naps but no night is legitimate (naps are separate sessions).

### 9.6 Derived values
| Value | Formula | Note |
|---|---|---|
| Time in bed | `(ed − st)/60` | epoch span is authoritative |
| Total sleep | `lt + dp + dt` | **not** `lt + dp` |
| Efficiency | `total_sleep / time_in_bed` | derived, not a field |
| Wake count | `wc` | **not** `wk` (awake *minutes*) |
| Awake minutes | `wk` or Σ mode-7 | they agree |

S2's field table labels `wk` "wake count" and omits `wc`; S4 proves the opposite
(`wk` always equals the summed mode-7 minutes, `wc` ≤ `wk`, varies
independently). S4 is right `[S4 · zepp_sleep.py]`.

---

## 10. Known gaps and dead ends
### 10.1 Verifiably not exposed
| Missing | Evidence |
|---|---|
| Raw IMU / accelerometer / gyro | cloud keeps processed metrics only `[S1]` |
| Exercise-level strength detail | `strength_group`/`strength_training_group` gives reps/set + `actionType`, but `actionType == 0` ("unrecognised") is ~80 % of sets and no lookup table exists `[S1]`; S7 matches sets to lap rows but names no exercise |
| Continuous SpO2 | only spot checks + nightly ODI; no per-minute series in S1–S7 |
| ECG / AFib waveform | only `readiness.afibScore`, `255` everywhere `[S1]`,`[S7]` |
| BP from the watch | only manual/app entries `[S2]`,`[S6]`,`[S7]` |
| Training-plan / Coach endpoint | 22 routes probed; plan state lives only in workout-row fields (`runningProgram`, `dailyScore`, `dailyPlanFinished`) `[S7]` |
| Per-second HR data | `fileInfo/events` is an index; the blob route was never found `[S1]`,`[S7]` |
| Standing/sedentary time, sleep regularity | no field identified `[S7]` |
| Sport code → name map | none; bare int `[S1]`,`[S7]` |
| Sleep-score breakdown | not exposed; only `ss` |

### 10.2 Dead ends, with what was tried
| Dead end | What happens | Verdict |
|---|---|---|
| `Charge/stress_data` as the stress series | `stressInfo` base64 blob; a protobuf decoder worked but yields a **dense** per-minute curve with no gaps; best fit vs app R²=0.40 | internal model; use `all_day_stress` `[S1]` |
| `refresh_token` | returned by phase 1, captured by `huami-token`, consumed by nobody; no renewal endpoint in any reference | re-login on 401 `[S1]`,`[S8]` |
| Plaintext password login | `POST /v2/client/login` with `emailOrPhone`/`password` → `HTTP 400 {"error_code":"0100"}` as of 2026-05-22; a scheduler had failed hourly for ≥13 h | dead; use encrypted phase 1 or a cookie `[S3 · README.md]` |
| Guessing `eventType` | capitalisation variants only worked for `respiratory`/`daily_health`; exact literals come from a preset list | use §4.3 and probe `[S1 · probe_events.py]` |
| Empty `200` as a negative test | a fabricated `eventType=ZZZ_NOT_REAL` also returns empty 200 | empty ≠ unsupported `[S7]` |
| `WatchSportStatistics` without params | `500` for real names, `400` for fabricated ones | pass all four params `[S7]` |
| Other sport slugs (`/v1/sport/walking/history.json`…) | `404`; only `run` is served | one call covers all sports `[S7]` |
| `region` at login | ignored; five variants were five identical requests | read `domains[]` `[S7]` |
| `x-hm-ekv: 1` on data GETs | encrypted `application/octet-stream` | omit it `[S2]` |
| `Emotion`, `blood_pressure/real_data`, `single_stress`, `LactateThreshold` | empty on some accounts, populated on others (LactateThreshold L1 in S7) | account/device dependent — probe |

### 10.3 Device-specific unknowns (T-Rex 3 Pro, RU)
Nothing in S1–S10 was captured on a T-Rex 3 Pro, or on a `-ru` host. Probe
before implementing:

1. Do `Charge/real_data` and `readiness/watch_score` exist (BioCharge/readiness)?
2. Does `slp` carry `supRem`/`supNap` true, and does `odd_stage` populate?
3. Is `data_hr` 1440 bytes, and `data` 3 or 8 B/min for the requested `byteLength`?
4. Which `eventType`/`subType` pairs return non-empty (§11)?
5. Which sleep-date convention this firmware uses (§9.2) — settle once, record it.
6. Sport codes (expect different from S1's `52/122/223`).
7. Whether `VO2_MAX`/`SPORT_LOAD` return anything, and the value field name
   (`vo2max`/`vo2Max`/`value` were all unconfirmed in S1).

### 10.4 Unverified register
Every `unverified` claim and what would confirm it.

| # | Claim | Confirm by |
|---|---|---|
| 1 | `api-mifit-ru.huami.com` serves the modern paths | one `band_data` call for yesterday → `code:1` |
| 2 | Phase 2 works on the RU host (vs a `-us2`/global login host) | try it; on 403/404 fall back to `api-mifit-us2.zepp.com` |
| 3 | RU portal is `user.huami.com`, not `user.zepp.com` | cookie's domain after login |
| 4 | `com.xiaomi.hm.health` coexists with the phone here | log in, confirm the phone still syncs |
| 5 | `heartRate` item shape and unit (ms vs s) | one call, inspect `items[0]` |
| 6 | `data` bytes 3–7 at `byteLength=8` | `sum(byte[2::8]) == stp.ttl`; diff 3- vs 8-byte blobs |
| 7 | `data`/`data_hr` length on DST days (1380/1440/1500) | `len(b64decode(...))` on 2026-03-29 and 2026-10-25 |
| 8 | Sleep date ownership / `stage[]` anchor on this firmware | the `st` fit test, logged for 7 nights |
| 9 | `dt` == REM here | `lt+dp+dt+wk == (ed−st)/60`, 7 nights |
| 10 | Nap grouping thresholds (adjacency, ≥85 min gaps) | 30 days of `odd_stage`, measure gaps |
| 11 | Overnight sleep filed in `odd_stage` with zeroed `ss/dp/lt` | scan 90 days for `stage==[] and odd_stage!=[]` |
| 12 | Skin temp is hundredths of °C of deviation | compare one night with the app |
| 13 | `skinTempBaseLine` sentinel `32767`/`-10` | observe an uncalibrated day |
| 14 | RMSSD field is `hrv`, SDNN is `sdnn` in ms | dump one item of each |
| 15 | Stress zone bounds (<40/40–59/60–79/≥80) | recompute the item's `*Proportion` strings |
| 16 | Stress cadence is per-minute, not 5-minute | compare `data[]` with the app's chart |
| 17 | `LactateThreshold/summary` exists on this watch | one call |
| 18 | `exertion/algo_result` exists on this watch | one call with `from=0` |
| 19 | `VO2_MAX` shape and value field name | one call with all four params |
| 20 | Body-comp field names (`fatRate`, …) | needs a real scale, or trust S7's external capture (L3) |
| 21 | `bloodPressure` / `manualData` shapes | one call each |
| 22 | `extra` is a JSON-string on this firmware | one `blood_oxygen/click` call |
| 23 | `spo2History` is 60 slots | same call |
| 24 | Sport code → name for this device | log a known activity, read `type` |
| 25 | History paging form (`data.next` vs `startTrackId`) | a page with >200 workouts |
| 26 | `sport_title` exists here | one history row |
| 27 | `auto_recognition` cleanly separates manual/auto here | 57/57 on S1's strap; recheck |
| 28 | Multisport via `parent_trackid`/`child_list` | needs a multisport recording |
| 29 | `stp.stage[].mode` names (3/4 vs S2's 1/3/7/76) | correlate stage cadence with a known walk |
| 30 | `tz`/`timeZone` are wrong for this account at DST | compare offsets across a transition |
| 31 | `/users/{id}/devices` served on this host | one call (never log `auth_key`) |
| 32 | `second_heart_rate` file route | unresolved in every source |

---

## 11. How to verify against the live account
### 11.1 Probe design
The approach that produced S1 and S7, worth reusing verbatim:

1. **One login per process**, token cached to disk (`0600`) with `user_id` and
   `cached_at`; `--force` is manual only.
2. **Land the raw payload before parsing** — `raw_ingest` keyed by
   `(source, day)`, committed before the parser runs. When a shape changes the
   parser throws, and without the raw the day is only recoverable by re-fetching
   something the server may no longer have `[S1 · docs/02-extract.md]`.
3. **Probe with a control.** For every "does this exist?" also ask something
   fabricated: fake `eventType`, fake statistic name, wrong host. The control
   tells you what "no" looks like on that route `[S7]`.
4. **Two independent quantities or it is not verified.** Daily steps
   (`band_data` vs `DailyHealth`), per-minute step bytes vs `stp.ttl`, readiness
   `sleepRHR` vs `slp.rhr`, `sleepHRV` vs mean RMSSD in `[st, ed]`, respiratory
   first non-zero minute vs sleep onset, `odi` vs `odiNum/(cost/3600)`. That is
   how every L1/L2 row in §5 was established `[S7 · capability-matrix.md]`.
5. **One day first, then a 30-day window** (~300 GETs, 4 workers).
6. **Never log** tokens, `deviceSn`, MACs, `auth_key`, or GPS. Redact at the
   writer.

### 11.2 Skeleton
```python
# probe.py — one endpoint at a time, raw first, control alongside
def probe(name, path, params, control=None):
    out = {}
    for label, p in (("real", params), ("control", control)):
        if p is None:
            continue
        r = SESSION.get(f"https://{HOST}{path}", params={"r": str(uuid.uuid4()), **p},
                        headers=HEADERS, timeout=30)
        store_raw(name, p, r.text)                      # BEFORE parsing
        j = r.json() if r.headers.get("content-type","").startswith("application/json") else None
        items = j.get("items") if isinstance(j, dict) else None
        out[label] = {"status": r.status_code, "bytes": len(r.text),
                      "keys": sorted(j)[:8] if isinstance(j, dict) else None,
                      "items": len(items) if isinstance(items, list) else None,
                      "item_keys": sorted(items[0])[:20] if items else None,
                      "value_keys": sorted(items[0].get("value") or {})[:20] if items else None}
        time.sleep(0.4)                                  # 429 is real
    print(name, json.dumps(out, ensure_ascii=False)); return out
```

Probe order: `huami.health.getUserInfo.json` → `band_data` (yesterday) →
`all_day_stress` → `Charge/real_data` → `readiness` → `HRVRMSSD` →
`RespiratoryRate` → `DailyHealth` → `WatchSportStatistics` with **and** without
params → `sport/run/history.json` → `weightRecords`.

### 11.3 Acceptance tests
| Endpoint | Passes when |
|---|---|
| `band_data` | `code==1`; one object per day; `len(b64decode(data_hr))==1440`; `summary` parses; `stp.ttl>0` on a walked day |
| `all_day_stress` | `items` non-empty; `item.timestamp % 86400000 == 0`; every `data[]` sample inside `[timestamp, timestamp+24h)`; aggregates equal recomputation |
| `Charge/real_data` | `s` multiples of 60000; `total` mostly ≠ 255; `total ≈ mean(mental, physical)` |
| `readiness` | `value.timestamp` on local midnight; `sleepRHR` == `slp.rhr`; `sleepHRV` ≈ RMSSD mean within a few ms |
| `HRVRMSSD` | samples inside the sleep window; mean ≈ `readiness.sleepHRV` |
| `RespiratoryRate` | 1440 bytes; first non-zero byte within ~5 min of sleep onset; zeros 900–1350 |
| `DailyHealth` | `totalSteps`/`totalCalories` == `stp.ttl`/`cal` on shared days |
| `SPORT_LOAD` | `dayId` parses; `currnetDayTrainLoad` present; `wtlSum ≥` it |
| `VO2_MAX` | any non-empty `items` — then record the value field name |
| `history` | `data.summary` non-empty; `type`, `auto_recognition` present; `data.next` readable |
| `detail` | `heart_rate` decodes to a monotonic `t_sec` and plausible bpm |
| `heartRate` | items non-empty; timestamps monotonic; values 30–220 |
| `weightRecords` | `summary.weight > 0`; `bmi` consistent or flagged |
| `blood_oxygen/click` | `extra` parses, `spo2` in 50–100 |
| `blood_oxygen/odi` | `odi ≈ odiNum/(cost/3600)` |

### 11.4 Reading a failed probe
| Result | Read as | Next step |
|---|---|---|
| `403` | wrong host for this account | §3.2 |
| `401` | expired token | one re-login; if it recurs, stop (lockout risk) |
| `429` | rate limited | back off with jitter; reuse the cached token |
| `404` | path absent on this deployment | check §4.1 — the path may postdate this host |
| `400` on `WatchSportStatistics` | unknown statistic name | not evidence about your device |
| `500` on `WatchSportStatistics` | name known, call incomplete | send all four params |
| empty `200` | **ambiguous** — no data or no feature | run the fabricated control; only a non-empty control makes an empty meaningful |
| non-JSON / `octet-stream` | you sent `x-hm-ekv: 1` | remove it |

Record the outcome per `(endpoint, day)` in a `sync_state` table so the next run
is incremental and a silently dead job shows up as a stale `last_ok_at`
`[S1 · docs/02-extract.md]`.

---

## §A Live verification log

Everything in this section was measured against a real account on **2026-11-08**
with `tools/zepp_probe.py`. Nothing here is copied from another project. Where
it contradicts an earlier section, this section is correct for this device.

### A.1 Account and host

| Fact | Value |
|---|---|
| Device | Amazfit T-Rex 3 Pro 48mm/44mm, `deviceSource` `10551553`, firmware `6.4.16.3` |
| Account region per login cookie | `de2` (`api-mifit-de2.zepp.com` in `domains[].cnames`) |
| Host that actually serves `band_data` | `https://api-mifit-ru.huami.com` |
| Second binding on the account | `deviceSource` `12`, firmware `V1.1.5.56`, last seen 2019-07 — a dead entry that still returns `bindingStatus: 1` |

Both `api-mifit-ru.huami.com` and `api-mifit-de2.zepp.com` returned byte-identical
responses for `band_data`, stress, readiness and Charge. Region discovery must
therefore probe for "which host has this account's devices", not trust the
cname alone; and it must not conclude that a second device is active just
because `activeStatus` is `1`.

### A.2 `band_data.data` — the record layout, measured

`len(data) == 4320` for every day → **3 bytes per minute**, 1440 minutes.

Channel sums for four consecutive days, against the totals in the same
response's `summary.stp`:

| Day | `stp.ttl` | byte 0 sum | byte 1 sum | byte 2 sum | byte 2 == `ttl` |
|---|---|---|---|---|---|
| 2026-11-05 | 1422 | 146540 | 16523 | **1422** | exact |
| 2026-11-06 | 7045 | 142360 | 31135 | **7045** | exact |
| 2026-11-07 | 11919 | 136584 | 35482 | **11919** | exact |
| 2026-11-08 | 2258 | 167146 | 10674 | **2258** | exact |

* **byte 2 is the per-minute step count.** Confirmed to the step across four
  days, and independently against the phone app's own display (the app showed
  `2 258 steps` for 2026-11-08; the decoded sum was 2258).
* **byte 0 is not distance and not calories.** It is non-zero in all 1440
  minutes with a maximum of 120-126, i.e. an always-present intensity-like
  channel.
* **byte 1 is unidentified.** It is non-zero in roughly the active minutes plus
  a tail (624-875 of 1440), it does not sum to `stp.dis` or `stp.cal` under any
  power-of-ten scaling. Do not present it as distance.
* `summary.byteLength` was **8** on every day while the blob is 3 bytes/minute.
  `byteLength` is *not* the record width; only `len(data) / 1440` is.
* Distance and calories therefore have **no per-minute source on this device**;
  they must be distributed proportionally to the step series, and the daily
  totals from `stp.dis` / `stp.cal` must be preserved exactly.

`stp.stage[]` (1-18 segments/day) and `stp.stepStageSummary[]` (17-59 entries)
are both populated, so a segment-level cross-check is available when the blob
cannot be decoded.

### A.3 `band_data.data_hr`

1440 bytes every day, with 561-1062 valid minutes (sentinels `0`/`254` for the
rest, i.e. the watch was off the wrist). Decoded minutes matched the app: 2026-11-08
09:30-12:00 local showed the same values the phone displayed, ending on the
`96 bpm` the app had on screen.

### A.4 Events: the `from`/`to` trap

| Query | Result |
|---|---|
| `?eventType=all_day_stress&limit=5` (no range) | 5 items, **oldest first** (2025-12-24) |
| `?eventType=all_day_stress&limit=5&reverse=true` | 5 items, **newest first** |
| `?eventType=all_day_stress&limit=5&from=<ms>&to=<ms>` | `{"items":[]}` |
| same, `from`/`to` in seconds | `{"items":[]}` |
| `/v2/users/me/events?eventType=readiness&subType=watch_score&from&to` | `{"items":[]}` |

Sending a range does not error, it returns an empty list — so a client that
always sends a window looks like an account with no data. `reverse=true` plus a
`limit` is the working shape.

Per-source results on this account (limit 10, newest first):

| Source | Endpoint | Items (limit 10) | Shape |
|---|---|---|---|
| `all_day_stress` | `/users/{id}/events` | 10 (316 total, 2025-12-24 → 2026-11-04) | `item.data` is a JSON **string** of `[{time, value}]`, absolute epoch ms; `avg/min/maxStress` at item level as strings |
| `blood_oxygen` | `/users/{id}/events` | 10 | `subType` `osa_event` or `click`; the reading lives in `extra`, a JSON **string** containing `spo2`, `spo2History[60]`, `isAuto` |
| `readiness` | `/v2/users/me/events` | 10 | `subType watch_score` → `value.rdnsScore`, `sleepHRV`, `sleepRHR`, `ahiScore`, `skinTempCalibrated`; `subType watch_score_data` → `value.rawData`, no scores |
| `HRVRMSSD` | `/v2/users/me/events` | 10 | `value.startTime` + `samples[].s` (ms offset), `samples[].hrv` |
| `RespiratoryRate` | `/v2/users/me/events` | 10 | `value.measurements` = base64, 1440 bytes, 1 byte/minute anchored at **UTC** midnight of `item.timestamp` |
| `Charge` | `/v2/users/me/events` | 10 | `value.startTime` + `samples[].s`, `total/mental/physical`, `total==255` = not computed |
| `PaiHealthInfo` | `/users/{id}/events` | 10 | `dailyPai`, `totalPai`; `item.timestamp` is local midnight **expressed in UTC** (23:00 of the previous day for CET) |

Empty on this account: `single_stress` (last item 2026-04-14), `Emotion`,
`EmotionBaseline`, `blood_pressure`, `LactateThreshold`, `body_battery`,
`Energy`, `HeartRate`, `spo2`, `sleep_score`. `sleep` and `daily_health`
return no items at all on `/users/{id}/events`.

### A.5 Readiness is two different shapes

`/v2/users/me/events?eventType=readiness` nests the fields under `value`:

```json
{"subType":"watch_score","value":{"rdnsScore":87,"rdnsInsight":39,
 "sleepHRV":74,"sleepRHR":50,"ahiScore":..,"skinTempCalibrated":..,
 "timestamp":1793746800000,"timestampUpdate":1793749740000}}
```

`/users/{id}/events?eventType=readiness` (v1) returns the *same* day with the
fields **flat on the item** and the `timestamp` key repeated twice in the JSON —
once with the compute time, once with the day anchor. `json.loads` keeps the
last occurrence, so a parser that reads `item["timestamp"]` gets the anchor by
accident, while one that reads `value["timestamp"]` gets nothing because `value`
does not exist. Accept both shapes.

`value.timestamp` (or the surviving `item.timestamp` in v1) is local midnight of
the scored day; `timestampUpdate` is when Zepp computed the score, usually the
following morning.

### A.6 Sleep is filed by **wake** date

| Row `date_time` | `slp.st` → local | `slp.ed` → local |
|---|---|---|
| 2026-11-07 | 2026-11-06 23:11 | 2026-11-07 07:26 |
| 2026-11-08 | 2026-11-08 00:23 | 2026-11-08 08:43 |

The 11-07 row contains a night that *started* on 11-06, so the record belongs to
the morning you woke up, not the evening you went to bed. This contradicts the
"sleep is assigned to the date you fell asleep" note repeated in several
projects; on this firmware the opposite is true. `dp + lt + dt` equals
`ed - st` to within the awake minutes, which makes `dt` the REM duration and
confirms `wk` as minutes awake (values 1-10) rather than a wake *count*
(`wc` 1-5 is the count).

### A.7 `WatchSportStatistics` stores only days with a value

`SPORT_LOAD` for 2026-10-09 → 2026-11-08 returned 10 rows, not 30: days without
training are simply absent. Field names: `dayId` (`YYYY-MM-DD`),
`currnetDayTrainLoad` (sic), `wtlSum`, `wtlSumOptimalMin`, `wtlSumOptimalMax`,
`wtlSumOverreaching`. `wtlSum` is a 7-day sum, not the day's load.

A two-day window — what the previous integration asked for — returns nothing on
most days, which is why the training-load sensors read `unknown`. Query a month
and take the newest row. `VO2_MAX` returned 0 rows for the same period;
`sport/run/history` returned 627 workouts and `weightRecords` 7.

### A.8 The event channels stopped on 2026-11-04

On 2026-11-08, every event-backed source had its newest item on **2026-11-04**
(stress, SpO2, readiness, HRV, respiratory, Charge, PAI, DailyHealth, exertion,
phn), while `band_data` was current to 2026-11-08 10:33 (`summary.sync`) with
steps, heart rate and sleep present for every day in between.

The watch reports firmware `6.4.16.3`; the Home Assistant device registry still
had `6.3.25.7` from setup time. The most economical explanation is a firmware
change on the watch around that date, but this is **not confirmed** — the point
of recording it is that "my sensors are frozen" can be a cloud-side fact and not
a bug in the client. A client that silently re-serves the last known value hides
this; exposing the measurement timestamp does not.

### A.9 Reproducing all of this

```bash
python3 tools/zepp_probe.py --apptoken <apptoken> --userid <id> --days 5 --out probe-out
```

Writes `probe-report.md`, the decoded per-day table above, and every raw
payload under `probe-out/raw/`. Only `aiohttp` is required.

### A.10 Items this log takes off the unverified list

Cross-referencing §10.4 against the live account above:

| §10.4 item | Now | Evidence |
|---|---|---|
| 1. `api-mifit-ru.huami.com` serving modern paths | **verified** | `band_data`, all events, `SPORT_LOAD`, `weightRecords`, `sport/run/history` all answered on that host |
| 4. `com.xiaomi.hm.health` coexisting with the phone | **not used** | The integration reads with `appname: com.huami.midong` and logs in with `com.huami.webapp`; the phone kept its session throughout |
| 6. `data` byte layout at `byteLength=8` | **resolved differently** | The blob is 3 bytes/minute, not 8; `byteLength` does not describe the record width (§A.2) |
| 8. Sleep date ownership on this firmware | **verified: wake date** | Two consecutive rows whose `st` falls on the previous evening (§A.6) |
| 9. `dt` == REM here | **verified** | `dp + lt + dt` reproduces `ed - st` within the awake minutes, on four nights |
| 22. `extra` as a JSON string | **verified** | `blood_oxygen` items carry `extra` as a string with `spo2`, `spo2History`, `isAuto` (§A.4) |
| 23. `spo2History` = 60 slots | **verified** | 60 zero-padded slots observed in every `osa_event` |
| 31. `/users/{id}/devices` on this host | **verified** | Returned both bindings, including the dead 2019 one |
| 11. Overnight sleep in `odd_stage` with zeroed fields | **not observed** | `odd_stage` carried naps only; nights arrived fully populated in `slp` |
| 7. `data` length on DST days | **still unverified** | No DST day fell inside the probe window; the decoder accepts 1380/1440/1500 |
