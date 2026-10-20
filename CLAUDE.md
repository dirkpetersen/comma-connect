# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

**comma-connect** is the web/mobile companion app for [openpilot](https://github.com/commaai/openpilot). It is a React SPA that lets users view drives, manage comma devices, stream live teleoperation, and manage Comma Prime subscriptions. Deployed at https://connect.comma.ai.

## Commands

```bash
pnpm install              # install deps (use pnpm, not npm/yarn)
pnpm start                # dev server at localhost:3000
pnpm build:development    # dev build
pnpm build:production     # production build (includes Sentry source maps)
pnpm lint                 # ESLint over src/
pnpm test                 # unit tests (Jest + SWC + jsdom)
pnpm test-coverage        # with coverage report
pnpm test-puppeteer       # E2E tests (Puppeteer, requires running server)
```

Run a single test file:
```bash
pnpm test src/__tests__/someFile.test.js
```

## Architecture

### Entry & routing

`src/index.jsx` mounts the Redux store + React Router. `src/App.jsx` does a single auth check — authenticated users see `ExplorerApp` (`src/components/explorer.jsx`), unauthenticated users see `AnonymousLanding` (`src/components/anonymous.jsx`).

URL structure parsed in `src/url.js`:
- `/:dongleId` → device dashboard
- `/:dongleId/:startTime/:endTime` → drive viewer with zoom
- `/:dongleId/:logId/:start/:end` → segment range view
- `/:dongleId/prime` → Prime subscription page
- `/:dongleId/stream` → live body teleoperation

URL segments drive Redux state (dongleId, zoom, primeNav, streamNav) via `src/actions/startup.js` on each navigation.

### State management

Redux store (`src/store.js`) with `redux-thunk`. The canonical state shape is defined in `src/initialState.js` — read it first when tracing data flow. Key slices:

- `dongleId` — currently selected device
- `zoom` — `{start, end}` ms timestamps of the selected drive view; **non-null zoom means a drive is being viewed**
- `currentRoute` — route metadata object for the active drive
- `routes` — list of routes for the selected device and time filter
- `devices` — user's comma devices
- `streamNav` — boolean: body teleop modal open
- `primeNav` — boolean: prime page active
- `loop` / `offset` — video playback position (ms from route start)

Reducers are in `src/reducers/globalState.js` (device/route/filter mutations) and `src/timeline/playback.js` (video seek/play/pause).

`Obstruction` (from `obstruction` package) is used instead of `reselect` for `mapStateToProps` — it takes an object mapping prop name → dot-path string into state.

### Component hierarchy

```
ExplorerApp (explorer.jsx)
  AppHeader
  AppDrawer         ← device list + navigation tabs
  Dashboard         ← drive list + device info (no zoom)
  DriveView         ← video + map + timeline (when zoom is set)
    DriveVideo / Media.jsx
    DriveMap
    Timeline        ← scrubber, thumbnails, ruler
  BodyTeleop        ← WebRTC streaming modal (streamNav=true)
  Prime/            ← lazy-loaded (primeNav=true)
```

The `ExplorerApp` component is the primary layout controller: it computes sidebar width, header height, and which content panel to render based on Redux state (`zoom`, `currentRoute`, `streamNav`, `primeNav`).

### Styling conventions

Two systems coexist:
- **Material-UI v1** (`@material-ui/core`) with `withStyles(styles)(Component)` HOC pattern — used in older/core components
- **Tailwind CSS v3** — used in newer components

Custom color palette is in `src/colors.js`; MUI theme in `src/theme.js`. Don't add new MUI components — prefer Tailwind for new UI work.

### API & auth

All comma API calls go through `@commaai/api` (external package). Auth is handled by `@commaai/my-comma-auth`. The auth token is a JWT stored via `AuthStorage`. **Demo mode** works by providing a hardcoded JWT — it surfaces most UI without a real device.

Runtime config (API root URLs) is injected by Nginx via `config.js.template` into `window.COMMA_URL_ROOT`, `window.ATHENA_URL_ROOT`, etc. In dev, these come from `.env.development`.

### Timeline / playback

`src/timeline/` handles all video playback state. The `offset` in Redux is milliseconds from the start of the current route. `src/timeline/segments.js` manages which route segments are loaded. Video sync with map happens inside `DriveVideo/Media.jsx`.

### Testing

Unit tests live in `src/__tests__/` and alongside source files (`.test.js`). E2E (Puppeteer) tests are in `src/__puppeteer__/`. The Jest config at `jest.config.js` uses SWC for transforms and `jsdom` for the DOM environment.

---

## Self-hosted AWS deployment

This instance is deployed independently of comma's infrastructure because the device owner is banned from comma's services. All drives are stored in a self-owned S3 bucket instead.

### Infrastructure (AWS account 454885954148, profile `dipeit`)

| Resource | ID / name | Purpose |
|---|---|---|
| S3 bucket | `comma-connect` (us-west-2) | Static site + drive storage (`drives/` prefix) |
| CloudFront | `E15SSGZKTAFZJK` → `d2mycvoqxvboq2.cloudfront.net` | Serves the SPA |
| Route 53 zone | `Z07875761ML7JE2RJBJ4` (`aws.internetchen.de`) | DNS for all AWS services |
| ACM cert | `258e7fa8-ee95-4113-b321-5f0fe9be759d` (us-east-1) | TLS for CloudFront |
| API Gateway | `jh69za4byd` (us-west-2) | Upload API endpoint |
| Lambda | `comma-uploader-api` (us-west-2) | Handles upload URL requests |
| IAM role | `comma-uploader-lambda` | Lambda → S3 PutObject on `drives/*` |

**Live URL:** https://comma-connect.aws.internetchen.de

### Redeploy frontend after code changes

```bash
# from /home/dp/gh/comma/comma-connect
node_modules/.bin/vite build

# upload hashed assets (long cache)
# ⚠️ The bucket ALSO holds all drive data (drives/, ~1 TB) and the Lambda's claims/ + users/ records.
#    With --delete, every prefix NOT excluded below is DELETED. Never drop these three excludes.
aws --profile dipeit s3 sync dist/ s3://comma-connect/ --delete \
  --exclude "drives/*" --exclude "claims/*" --exclude "users/*" \
  --cache-control "public, max-age=31536000, immutable" \
  --exclude "index.html" --exclude "sw.js" --exclude "workbox-*.js" \
  --exclude "manifest.webmanifest" --exclude "config.js"

# upload no-cache entry points
for f in index.html sw.js manifest.webmanifest config.js workbox-*.js; do
  aws --profile dipeit s3 cp dist/$f s3://comma-connect/$f \
    --cache-control "no-cache, no-store, must-revalidate"
done

# 2026-09-27 lockdown: CloudFront may read ONLY the web-app paths (bucket-policy allowlist: index.html,
# config.js, sw.js*, workbox-*, manifest.*, robots.txt, favicon.*, icon-*, assets/*, images/*), and a
# `drives/*` behaviour (CloudFront Function comma-connect-deny-drives) returns 403. A NEW root file name
# from a build must be added to the bucket policy, or CloudFront silently serves index.html instead.
# After each deploy, re-run the probe: /home/dp/gh/comma/_scratch/aws-lockdown-backup-20260927/probe.sh
# Details + rollback: /home/dp/gh/comma/docs/AWS-COST-2026-09.md §6c.

# bust CloudFront edge cache
aws --profile dipeit cloudfront create-invalidation \
  --distribution-id E15SSGZKTAFZJK --paths "/*"
```

### Drive upload pipeline

The comma device (`dongle_id: 2fd850c60cc5bfef`) uploads drives to our Lambda instead of comma's servers. The upload API mimics `api.comma.ai` with three endpoints:

| Endpoint | Purpose |
|---|---|
| `GET /v1.4/{dongle_id}/upload_url/` | Returns presigned S3 PUT URL; drives land in `s3://comma-connect/drives/{dongle_id}/{segment}/{file}` |
| `GET /v1.1/devices/{dongle_id}` | Returns minimal device record so openpilot doesn't abort |
| `GET /v1/me` | Returns minimal user identity |

Auth: the device sends `Authorization: JWT {device_jwt}`. The Lambda checks the `identity` claim matches the dongle_id in the URL path **and verifies the RS256 signature** against the device public key (`DEVICE_PUBKEYS`), controlled by `AUTH_SIG_MODE` — see "Device JWT signature check" below. (The dongle id is public, so identity matching alone let anyone mint a token and get presigned PUTs.)

### Device JWT signature check (`AUTH_SIG_MODE`)

openpilot (`common/api.py`) signs `{identity, nbf, iat, exp}` with `/persist/comma/id_rsa` using **RS256**. `verify_device_jwt` pins `alg=RS256` (rejects `none`/HS256), verifies with the stdlib `_rs256_verify` against the dongle's key, then requires `identity == dongle`, `exp` present and not past (300 s skew), `nbf` not in the future (300 s skew). Covered routes (all that call `check_auth`): `GET /v1.4/{dongle}/upload_url/` and `GET /v1(.1)/devices/{dongle}`. **Not covered:** the device variant of `GET /v1/me` (decodes the token unverified; returns only a fixed stub, no S3 access).

Env vars (Lambda `comma-uploader-api`):
- `DEVICE_PUBKEYS` — JSON `{"<dongle>": {"n": "<b64url>", "e": "<b64url>"}}`. Public key only. Generate with `uploader-api/tools/device_pubkey_env.py` (no network); never commit the output.
- `AUTH_SIG_MODE` — `log` (default if unset): a failed check is logged and the request proceeds as before. `enforce`: failed check returns 401. Any other value is treated as `enforce` (fail closed).

Log lines (CloudWatch `/aws/lambda/comma-uploader-api`; the token is never logged):
`AUTH_SIG_FAIL reason=<bad_sig|bad_alg|expired|no_exp|not_yet_valid|bad_identity|malformed*> dongle=.. route=.. mode=.. action=allow|reject`,
`AUTH_SIG_NOKEY` (no key for that dongle), `AUTH_SIG_CONFIG_ERROR` (DEVICE_PUBKEYS is not valid JSON).

Rollout: (1) deploy code + `DEVICE_PUBKEYS`, `AUTH_SIG_MODE=log`; (2) let the real device upload through at least one drive plus a few `upload_url` polls and confirm CloudWatch shows **zero** `AUTH_SIG_FAIL` / `AUTH_SIG_NOKEY` for it (and that requests are arriving at all); any hits from other sources are forgery attempts; (3) set `AUTH_SIG_MODE=enforce`, drive again, confirm uploads still 200. Rollback: set `AUTH_SIG_MODE=log` (config only), or `update-function-code` with the previous zip (kept privately; the function had no versions/aliases before this change — version 1 was published with the signature-check deploy, so there is no earlier version to repoint to). Keys must be RSA e=65537, n >= 2048 bits, else `AUTH_SIG_CONFIG_ERROR` and that dongle counts as keyless. **`update-function-configuration --environment` REPLACES all variables** — read the current ones first (`get-function-configuration`) and send the full merged set.

**Lambda source:** `uploader-api/handler.py` (in this repo — kept in sync with the deployed function)

To update Lambda code:
```bash
cd uploader-api
zip -r function.zip handler.py
aws --profile dipeit lambda update-function-code \
  --region us-west-2 --function-name comma-uploader-api \
  --zip-file fileb://function.zip
```

To update the allowed dongle_id:
```bash
aws --profile dipeit lambda update-function-configuration \
  --region us-west-2 --function-name comma-uploader-api \
  --environment "Variables={S3_BUCKET=comma-connect,ALLOWED_DONGLES=<dongle_id>}"
```

CloudWatch alarm on rejected requests (documented, NOT executed; only fires in `enforce` mode because `action=reject` is logged only then — in `log` mode watch `action=allow` lines by hand):
```bash
aws --profile dipeit --region us-west-2 logs put-metric-filter \
  --log-group-name /aws/lambda/comma-uploader-api --filter-name auth-sig-reject \
  --filter-pattern '"action=reject" ?"AUTH_SIG_FAIL" ?"AUTH_SIG_NOKEY"' \
  --metric-transformations metricName=AuthSigReject,metricNamespace=CommaConnect,metricValue=1,defaultValue=0
aws --profile dipeit --region us-west-2 cloudwatch put-metric-alarm --alarm-name comma-uploader-auth-sig-reject \
  --namespace CommaConnect --metric-name AuthSigReject --statistic Sum --period 300 --evaluation-periods 1 \
  --threshold 1 --comparison-operator GreaterThanOrEqualToThreshold --treat-missing-data notBreaching \
  --alarm-actions arn:aws:sns:us-west-2:454885954148:pnw-waze-budget-alert
```

### Device configuration (comma 3X — pnw-pilot git checkout)

> **History note:** these started as manual on-device patches in the overlay era (June 2026), with a
> "re-apply after every update" caveat. That era is over — `/data/openpilot` is now a **git checkout
> auto-tracking `origin/3devpnw`** of `dirkpetersen/pnw-pilot`, and everything below is **committed
> on the pnw branches**, so it survives updates and reinstalls automatically. No manual re-apply.

Committed device-side integration (all in `~/gh/comma/pnw/pnw-pilot`):

- **`common/api.py`** — `API_HOST` default points at our gateway
  (`https://jh69za4byd.execute-api.us-west-2.amazonaws.com`); also exported in `launch_env.sh` as a
  belt-and-suspenders fallback.
- **`system/loggerd/uploader.py`** — the full `connect2pnw` two-pass uploader: pass 1 = small files
  (qlog/qcam), pass 2 = HD video + rlog, gated to real external WiFi + non-metered + **offroad-only**
  (driving-time uploads caused control-loop lag). One HD upload interleaved per 4 small
  (`PASS2_INTERLEAVE`). `_get_local_ip()` sends the LAN IP as `local_ip` on every `upload_url`
  request (the device locator). `DeferHDVideoUpload` param holds HD on precious WiFi.
- **`system/loggerd/deleter.py`** — preferentially preserves segments with un-uploaded large files.
- Sidebar/UI — CONNECT is WiFi-only; green = pass-1 uploading, blue + Mbps (`FirehoseSpeed`) = pass-2.

⚠️ **The silent-data-loss gotcha (still applies):** if `API_HOST` is ever lost (bad reflash from a
branch without the patch + env unset), the device falls back to comma's API, which returns **HTTP
412** to every upload request. openpilot treats 412 as success, stamps files `user.upload=1`, and
they are **deleted without ever reaching S3**. Symptom: `upload_ignored` floods swaglog while S3
stays quiet. Details + recovery: `~/gh/comma/docs/DEVICE-STATE.md` (Known Gotchas).

### Auth0 authentication (user login: Google / GitHub / LinkedIn)

The SPA supports Auth0 login for the self-hosted deployment (`src/auth0.js`; buttons in
`src/components/anonymous.jsx`). It activates when `config.js` sets `AUTH0_DOMAIN` +
`AUTH0_CLIENT_ID` (+ `AUTH0_AUDIENCE`); empty values = legacy comma auth (unchanged). The Auth0
access token is stored via my-comma-auth's storage, so `Request.configure`/`isAuthenticated`/
`logOut` all work untouched.

The Lambda (`uploader-api/handler.py`) verifies `Authorization: Bearer` tokens fully — RS256
signature against the tenant JWKS (pure stdlib, cached 6 h) + iss + aud + exp — when env vars
`AUTH0_DOMAIN` + `AUTH0_AUDIENCE` are set (unset = user realm disabled). The device realm
(`Authorization: JWT`, upload pipeline) is completely separate and unchanged.

**Device claim flow** (`POST /v1/claim {dongle_id}`, Bearer-authed): a user may claim a dongle iff
that device has uploaded to our bucket AND nobody claimed it yet (first-claim-wins). The set of
unclaimed devices is never listed — the claimant must know their device's ID; "no such device" and
"already claimed" return the same 403 so IDs can't be probed. State: `s3://comma-connect/users/
{sub}.json` + `claims/{dongle_id}.json` (admin unclaim = delete the claim object).
`GET /v1/me` with a Bearer token returns the profile + claimed dongles.

**Tenant setup (one-time, in the Auth0 dashboard):** create a SPA application (callback
`https://comma-connect.aws.internetchen.de/auth0-callback` + `http://localhost:3000/auth0-callback`,
matching logout + web-origin URLs), create an API (identifier = the `AUTH0_AUDIENCE` value), enable
the `google-oauth2`/`github`/`linkedin` connections (Auth0 dev keys work for testing; register own
OAuth apps for production). Then fill `public/config.js` + redeploy the frontend, and set
`AUTH0_DOMAIN`/`AUTH0_AUDIENCE` on the Lambda:

```bash
aws --profile dipeit lambda update-function-configuration \
  --region us-west-2 --function-name comma-uploader-api \
  --environment "Variables={S3_BUCKET=comma-connect,ALLOWED_DONGLES=2fd850c60cc5bfef,AUTH0_DOMAIN=<tenant>.us.auth0.com,AUTH0_AUDIENCE=<api-identifier>}"
```

### Waze police-alert proxy (AWS Lambda `comma-waze-proxy`)

Fleet-scale Waze police alerts with **no per-device API key** — design in `WAZE-API.md`. Devices
`GET /alerts?lat=..&lon=..` (keyless) against the existing API Gateway; the Lambda holds the one
shared upstream key (env `WAZE_KEY`) and caches transformed results in DynamoDB per quantized
~5.5 km cell (TTL 180 s), so the whole fleet shares one upstream Waze call per cell per window.
Runtime is tiny (warm ≈ 100 ms, cold ≈ 5 s one-off; 8 s timeout ceiling). Device side:
`wazeproxy2pnw` in pnw-pilot (proxy = default; `police_proxy.json` key = direct fallback — see
`pnw-pilot/docs/pnw/WAZE-API-KEY.md` for the personal-key user guide; direct mode has no budget
tracking).

**Upstream (migrated 2026-08):** RapidAPI's `waze-api` listing was retired; the proxy now calls
**OpenWebNinja PAYG** (`https://api.openwebninja.com/waze/alerts-and-jams`, header `x-api-key`,
$0.005/call). Alerts are at `data.alerts[]` (`alert_id`/`type`/`latitude`/`longitude`/
`publish_datetime_utc`/`street`/`city`) — there's no direction field upstream anymore, so the
normalized `magvar` is always `None` (the device already tolerates that).

**Spend caps + monitoring (deployed 2026-08-13)** — see `WAZE-API.md` §14 for the full detail:
a $25/mo global budget (402 on a cache-MISS once exhausted) and a 750/day per-device cap (429),
both DynamoDB-counter-based and checked only on a cache MISS; plus an hourly Lambda that emails
$5/$20 early-warnings since OpenWebNinja spend isn't visible to native AWS Budgets.

| Resource | Name / ID |
|---|---|
| Lambda | `comma-waze-proxy` (us-west-2, handler `waze_handler.handler`, source `uploader-api/waze_handler.py`) |
| DynamoDB cache | `comma-waze-cache` (PAY_PER_REQUEST, PK `cell`, TTL attr `exp`; also holds the `budget:YYYY-MM` / `dev:{id}:YYYY-MM-DD` / `alerted:YYYY-MM:<lvl>` counter items) |
| API route | `GET /alerts` on API Gateway `jh69za4byd` (integration `gw1z3tq`) → this Lambda |
| IAM role | `comma-waze-lambda` (DynamoDB RW on the cache table + logs; **no S3** — isolated from the uploader) |
| Budget-checker Lambda | `waze-budget-checker` (handler `waze_budget_checker.handler`, source `uploader-api/waze_budget_checker.py`) |
| Budget-checker role | `waze-budget-checker-role` (DynamoDB read on `comma-waze-cache` + `sns:Publish` + logs) |
| SNS topic | `pnw-waze-budget-alert` (`arn:aws:sns:us-west-2:454885954148:pnw-waze-budget-alert`), email subscription `dipeit@gmail.com` |
| EventBridge rule | `pnw-waze-budget-hourly` (`rate(1 hour)`) → invokes `waze-budget-checker` |

```bash
# update Lambda code
cd uploader-api && zip -q waze.zip waze_handler.py
aws --profile dipeit lambda update-function-code --region us-west-2 \
  --function-name comma-waze-proxy --zip-file fileb://waze.zip

# rotate the shared Waze key (env var) — also carries the budget-cap env vars (see WAZE-API.md §14)
aws --profile dipeit lambda update-function-configuration --region us-west-2 \
  --function-name comma-waze-proxy \
  --environment "Variables={WAZE_TABLE=comma-waze-cache,WAZE_KEY=<key>,WAZE_BUDGET_USD=25,WAZE_COST_PER_CALL=0.005,WAZE_DEVICE_DAILY=750}"

# verify: two GETs to the same cell → 2nd is a fast cached HIT
curl -s "https://jh69za4byd.execute-api.us-west-2.amazonaws.com/alerts?lat=47.6&lon=-122.33" -w '\n%{time_total}s\n'

# update the budget-checker Lambda code
cd uploader-api && zip -q checker.zip waze_budget_checker.py
aws --profile dipeit lambda update-function-code --region us-west-2 \
  --function-name waze-budget-checker --zip-file fileb://checker.zip

# change warning thresholds / caps
aws --profile dipeit lambda update-function-configuration --region us-west-2 \
  --function-name waze-budget-checker \
  --environment "Variables={WAZE_TABLE=comma-waze-cache,SNS_TOPIC_ARN=arn:aws:sns:us-west-2:454885954148:pnw-waze-budget-alert,WARN_USD_LEVELS=5\,20,BUDGET_USD=25,COST_PER_CALL=0.005}"

# send a TEST alert email (doesn't touch thresholds/flags)
aws --profile dipeit lambda invoke --function-name waze-budget-checker \
  --payload '{"test":true}' --cli-binary-format raw-in-base64-out /tmp/out.json
```

> The `waze-proxy/` Cloudflare Worker in the repo is the original implementation of the same
> design, kept as a portable alternative — **not** the deployed path (we host on AWS to avoid a
> second vendor). Optional `PROXY_SECRET` env var turns on the rotatable `x-pnw-auth` gate.

### Checking upload progress

```bash
# count files in S3
aws --profile dipeit s3 ls s3://comma-connect/drives/2fd850c60cc5bfef/ --recursive | wc -l

# files still pending on device
ssh comma@192.168.13.154 'find /data/media/0/realdata -type f ! -name "*.lock" | wc -l'

# uploader process running?
ssh comma@192.168.13.154 'pgrep -a -f uploader'
```

### Client IP logging

The Lambda handler prints a `CLIENT_IP` line to CloudWatch on every request, capturing **both** IPs:

```
CLIENT_IP src_ip=207.55.8.10 local_ip=10.16.116.31 method=GET path=/v1.4/2fd850c60cc5bfef/upload_url/
```

- **`src_ip`** — the device's **public** WAN IP (`requestContext.http.sourceIp`), i.e. the home
  router's address after NAT. AWS sees this on every request automatically.
- **`local_ip`** — the device's **internal** LAN IP (e.g. `10.16.x.x`). AWS cannot see this on its
  own; the device reports it. The uploader was patched to send it as a `local_ip` query param on the
  `upload_url` request (see device patch below). It changes when the device gets a new DHCP lease.

Nothing else records the client IP: API Gateway access logging is off, S3 server access logging is
off, and there is no CloudTrail trail.

```bash
# latest connecting IPs (last hour)
aws --profile dipeit logs filter-log-events --region us-west-2 \
  --log-group-name /aws/lambda/comma-uploader-api \
  --filter-pattern 'CLIENT_IP' \
  --start-time $(( ($(date +%s) - 3600) * 1000 )) \
  --query 'events[*].message' --output text

# just the most recent internal IP
aws --profile dipeit logs filter-log-events --region us-west-2 \
  --log-group-name /aws/lambda/comma-uploader-api \
  --filter-pattern 'CLIENT_IP' \
  --start-time $(( ($(date +%s) - 3600) * 1000 )) \
  --query 'events[-1].message' --output text
```

**Device patch (`local_ip` reporting):** `/data/openpilot/system/loggerd/uploader.py` has a
`get_local_ip()` helper (a UDP-connect-to-`8.8.8.8` trick to read the primary interface address) and
`do_upload()` passes `local_ip=get_local_ip()` to the `upload_url` request. Like the `api.py` and
`process_config.py` patches, this is overwritten by an openpilot overlay update — **re-apply it after
any update.** The matching Lambda side reads `queryStringParameters['local_ip']` in the `CLIENT_IP`
print.

## Curve DB pull routes (device JWT, signature ALWAYS enforced)

`GET /v1/curvedb/{dongle}/approved` and `POST /v1/curvedb/{dongle}/report` (design: workdir `docs/CURVEDB-CLOUD-DESIGN.md` §2.4).
They ignore `AUTH_SIG_MODE` (no log mode): dongle must be in `CURVEDB_DONGLES` and the `Authorization: JWT <token>` header must verify
(RS256 pinned, signature, identity == dongle, exp). Any failure is `401 unauthorized`. Code only here; no curve DB data, keys or dongle ids.

Contract (the device daemon `system/curvedb/curvedb_updated.py` uses exactly this):
- GET approved: 200 `{sha256, rows_bytes, manifest_sha256, manifest_bytes, build, approved_utc, rows_url, manifest_url}`; URLs are presigned GET, 300 s, for
  `curvedb/tables/<sha256>/curvedb_v2_rows.json.zst` and `.../manifest.json` (keys are built from `sha256`, never from pointer key strings).
  404 `{"error":"none published"}` only when `curvedb/approved.json` is missing (NoSuchKey). Any other S3 error, unparseable or malformed pointer: 503.
- Pointer `s3://<CURVEDB_BUCKET>/curvedb/approved.json` MUST contain: `sha256` (64 hex), `manifest_sha256` (64 hex), `rows_bytes`, `manifest_bytes`
  (non-negative ints), `build`, `approved_utc` (strings). NOTE: the current cloud-job `shadow/approved.json` has only sha256/rows_key/manifest_key/built_utc/rows,
  so the approve script (phase 3) must write the extra fields (`rows_bytes` = manifest.json `bytes`; hash/size of manifest.json). Until then the route returns 503 (logged `CURVEDB_POINTER_MALFORMED`).
- POST report: body <= 2048 bytes JSON object, only keys `live_sha, pointer_sha, last_auto_sha` (64 hex or null), `auto_utc`, `git_sha` (<= 64 chars of `[A-Za-z0-9:._+-]`),
  `loaded` (bool), `state` (disabled|waiting_clock|waiting_unmetered|none_published|current|installed|held_hand_change|refused|error), `reason` (<= 200 chars, non-printable -> `?`),
  `free_mb` (int >= 0). Else 400. Writes `curvedb/device/<dongle>/report.json` (+ `received_utc`), logs `CURVEDB_REPORT dongle=.. state=.. live=<sha8>`. Returns 200 `{"ok":true}`; S3 write error 503.
- Logs never contain tokens or presigned URLs.

Deploy (main session; NOT executed by the coder):
1. Attach this inline policy to role `comma-uploader-lambda` (backup bucket, not comma-connect):
```json
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":"s3:GetObject","Resource":["arn:aws:s3:::comma-curvedb-backup-454885954148/curvedb/approved.json","arn:aws:s3:::comma-curvedb-backup-454885954148/curvedb/tables/*"]},
 {"Effect":"Allow","Action":"s3:PutObject","Resource":"arn:aws:s3:::comma-curvedb-backup-454885954148/curvedb/device/*/report.json"}]}
```
   `aws --profile dipeit iam put-role-policy --role-name comma-uploader-lambda --policy-name curvedb-pull --policy-document file://policy.json`
2. Env: read the current set (`get-function-configuration --query Environment`), then send the FULL merged set (`--environment` REPLACES) adding
   `CURVEDB_DONGLES=<dongle>` and `CURVEDB_BUCKET=comma-curvedb-backup-454885954148` (the default already equals this; set it anyway so it is explicit).
   `DEVICE_PUBKEYS` must contain the dongle's key (the routes fail with `nokey` otherwise).
3. Code: `zip function.zip handler.py`, `update-function-code --zip-file fileb://function.zip --publish` (note the new version number; record the previous one first with `list-versions-by-function`).
   API Gateway must route `GET`/`POST /v1/curvedb/{proxy+}` (check the existing route is `$default`/proxy; if per-route, add both).
4. Verify: forged token -> 401; real device token -> 404 `none published` (or 200 once a pointer exists); CloudWatch shows `CURVEDB_AUTH_FAIL` only for forgeries.
5. Rollback: repoint to the previous published version (alias, or `update-function-code` with the previous zip). The routes are inert without `CURVEDB_DONGLES`: removing that env var (merged set) disables them with no code change.
