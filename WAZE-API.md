# WAZE-API.md — Fleet-scale Waze alerts via an on-demand caching proxy (no Lambda, no cron)

**Status:** IMPLEMENTED (2026-07-12). The Worker lives at `waze-proxy/` in this repo; the device
side is branch `wazeproxy2pnw` in pnw-pilot (proxy is the DEFAULT source — keyless; a
`police_proxy.json` with key+url still forces legacy direct). Deviations from this design, forced
by facts on the ground:
- **Workers KV instead of the Cache API** — `caches.default` is a no-op on `*.workers.dev`
  domains, and `internetchen.de` DNS is on IONOS (not Cloudflare), so no custom domain / no Cache
  API. KV free tier (100k reads / 1k writes per day) comfortably covers the fleet; an in-isolate
  L1 map cuts KV reads further.
- **Upstream bbox widened by Q/2** (Gemini review): the cache cell center can sit half a cell from
  the car, so the Waze query over-covers to keep the device's full ±0.30° view populated.
- **Stale proxy body → `nodata`, not empty-ok** (Gemini review): §6's "treat as empty" would show
  a false "Clear"; the device raises instead, keeping the never-false-clear invariant.
- **Error tag in-body**: upstream failures return HTTP 200 with `alerts: []` **plus an `error`
  tag**; the device surfaces the tag (e.g. `upstream 429`) on the red err line, state `nodata`.
- §9 (S3 write-through) and a custom domain remain unimplemented options.

The original proposal follows, kept as the design record.

**Owner problem (verbatim):** *"having a Waze API key is not clever and doesn't scale. Move the Waze
query to our personal Comma Connect platform that also maintains all the drive videos. Write a proposal
implementation for an API endpoint that allows us to query Waze. It should all be written in software
that doesn't require AWS Lambda and just lives in S3, so S3 serves as a web server."*

> **Pivot note (read this first).** The literal "just lives in S3" reading was explored and **rejected**
> — see §10. **S3 alone cannot do this.** S3 serves only bytes that *already exist*; it cannot query or
> transform Waze at request time. A pure-static design therefore forces one of two losing choices:
> **(a)** precompute *every possible* location (wasteful — the vast majority of tiles are never driven),
> or **(b)** refresh on a **schedule** (a GitHub Actions cron), which burns Actions minutes and hits
> cron-granularity / quota walls. Static and on-demand are mutually exclusive.
>
> The right shape is **on-demand + lazy**: fetch a location only when a car actually drives it, and
> cache the result so the whole fleet dedups onto one upstream call. That needs a *tiny* amount of
> compute **at request time** — but explicitly **NOT AWS Lambda** and **NOT a server the user has to
> stand up and babysit**. The recommendation below (a Cloudflare Worker) satisfies "not Lambda / nothing
> to maintain" while giving true on-demand. S3 can still play a role as an *optional write-through cache*
> for the hot path (§9) — as a cache, not the source of truth.

---

## 1. Why today's per-device design does not scale

Each device polls RapidAPI's Waze proxy **directly** from
`system/location_services/location_servicesd.py`:

- `DEFAULT_PROXY` (lines 57-61) ships a shared RapidAPI key in-distribution:
  - `url  = "https://waze-api.p.rapidapi.com/alerts"`
  - `host = "waze-api.p.rapidapi.com"`
  - `key  = "d5e52230…fcd5d"` (a BASIC-plan key)
- `PROXY_CFG = "/data/pnw/location/police_proxy.json"` overrides it (`_load_cfg()`, lines 205-214):
  the override file wins if it has `key` + `url`, else fall back to `DEFAULT_PROXY`.
- `PoliceUpdater._poll()` (lines 225-248) builds the request:
  - bbox = `POLICE_BBOX_DEG = 0.30` (~±20 mi) around `LastGPSPosition`, sent as query params
    `bottom-left = "{lat-0.30},{lon-0.30}"` and `top-right = "{lat+0.30},{lon+0.30}"` (URL-encoded).
  - headers `x-rapidapi-host` / `x-rapidapi-key`.
- Cadence: `POLICE_POLL_S = 60.0` (≤1/min), 20 s timeout, exponential backoff to
  `POLICE_MAX_BACKOFF_S = 15 min` on failure.

**The scaling failure is structural.** Waze calls = **N devices × once/min/drive**. Two cars on the same
road issue the same query twice. The shared BASIC key's **monthly** quota is already exhausted → HTTP 429
→ every device shows `state: "nodata"` ("Police —"). Adding devices strictly worsens it, and the key
ships inside the repo.

**What the device actually consumes** (the exact contract any replacement must preserve). `_poll()` keeps
only `a.get("type") == "POLICE"` and maps each alert to this raw dict:

```
{ "lat": float(a["locationY"]),   "lon": float(a["locationX"]),
  "magvar": a.get("magvar"),      "ts":  a.get("timestamp"),      # Waze epoch-MILLIS
  "uuid": a.get("uuid") or a.get("id"),
  "street": a.get("street") or "", "town": a.get("city") or "" }
```

That list is cached by `PoliceUpdater` and read via `snapshot()`. **The geometry is done on-device**, not
by Waze: `_line_police()` (lines 336-352) ages reports against `POLICE_STALE_S` (45 min) via `_age_min()`
on the epoch-ms `ts`, picks the nearest-ahead with `geo.nearest_ahead(path, …)`, derives a direction hint
from `magvar` (`_police_dir()`), and publishes a compact `police` block into the `LocationServices` mem
param (`/dev/shm/params`). The UI (`selfdrive/ui/onroad/location_services_status.py`) reads only that
block's `state` (`alert`/`clear`/`nodata`), `dist_mi`, `dir`, `age_min`, `uuid`, `town` — it never sees a
Waze payload.

**Design consequence:** a replacement source only has to reproduce **the raw-alert list**
(`{lat, lon, magvar, ts, uuid, street, town}`). Everything downstream — staleness, nearest-ahead, the
blue "POLICE AHEAD" banner, siren — is untouched. It's a drop-in swap at `PoliceUpdater._poll`, nothing
else. And because the device just sends its current position, **no on-device geohash helper is needed**.

---

## 2. Proposed architecture (recommended): on-demand caching reverse proxy

**One small edge proxy holds the single shared key, calls Waze on a cache MISS, and caches the
transformed result with a short TTL. Devices make a keyless GET for their current position.**

Three roles, and the one secret never leaves the proxy:

- **Proxy** (has the key, runs at the edge — a Cloudflare Worker, §3): on a request it computes a cache
  key from the (quantized) position + a short time bucket; on a **hit** it returns cached JSON with zero
  upstream calls; on a **miss** it calls Waze once, transforms to the device's raw-alert shape, caches it,
  and returns it.
- **Device**: `GET https://waze.<domain>/alerts?lat=..&lon=..` — **no key, no signing** — and parses the
  same raw-alert list it builds today.
- **Waze upstream** (RapidAPI): hit only on cache misses.

```
   many devices, same road            EDGE PROXY (Cloudflare Worker — NOT Lambda)
   ┌───────────┐                      ┌──────────────────────────────────────────────┐
   │ Model S   │──GET ?lat&lon──┐     │ key = quantize(lat,lon) + time-bucket(TTL)     │
   ├───────────┤                ├────▶│ caches.default.match(key) ──HIT──┐            │
   │ Lightning │──GET ?lat&lon──┘     │            │ MISS                 │            │
   └───────────┘   (no key)          │            ▼                      │            │
        ▲                             │   fetch Waze (x-rapidapi-*) ──────┼──▶ Waze / RapidAPI
        │                             │   transform → raw-alert JSON      │            │
        │      raw-alert JSON         │   cache.put(key, max-age=TTL) ◀───┘            │
        └──────────────────────────────  return JSON ◀────────────────────────────────┘
                                      └────────────────────────────────────────────────┘
   Waze calls per TTL window  ≈  # UNIQUE cache cells driven      (independent of device count)
```

**This is the scaling fix.** The cache **dedups the whole fleet onto one key per cell per TTL window**, so
Waze calls ≈ *unique cells actually driven per TTL*, **independent of device count**. It is **lazy**: only
areas a car actually visits are ever fetched — **no precompute, no cron, no wasted tiles**.

Reuses the platform that already stores drives (`comma-connect/CLAUDE.md`): same account/domain family
(Route 53 zone `aws.internetchen.de` — could add `waze.internetchen.de`); the drives bucket and its
Lambda (`comma-uploader-api` behind API Gateway `jh69za4byd`) are **left alone** — this feature does
**not** touch the Lambda upload path.

---

## 3. Recommended host: Cloudflare Worker

Why a Worker over the other options:

- **NOT AWS Lambda; nothing to run or maintain.** Code runs per-request at Cloudflare's edge; no server,
  no container, no OS to patch.
- **Free tier = 100k requests/day.** It will **not wall** the way GitHub Actions cron minutes do (§10).
- **Built-in caching** (`caches.default` / the Cache API, optionally Workers KV) — the dedup mechanism is
  native, no external cache to run.
- **HTTPS + a real hostname** out of the box (`waze.<domain>`), reachable from the car over LTE.
- **You already use Cloudflare — this is not a new vendor.** `comma-connect/deploy-preview.sh` deploys
  previews with `wrangler pages deploy dist --project-name=connect` (Cloudflare Pages,
  `connect-d5y.pages.dev`). `wrangler` and a Cloudflare account are already in the toolchain; a Worker is
  the same tooling, one more `wrangler deploy`.

### 3.1 Worker sketch (~30 lines)

```js
// waze-proxy Worker. Holds the ONE shared RapidAPI key (wrangler secret WAZE_KEY). ~30 lines.
const WAZE_URL  = "https://waze-api.p.rapidapi.com/alerts";
const WAZE_HOST = "waze-api.p.rapidapi.com";
const TTL_S     = 180;                        // police reports persist minutes; 2–5 min is fine (see §6)
const BBOX_DEG  = 0.30;                        // match the device's POLICE_BBOX_DEG
const Q         = 0.05;                        // cache-key quantization (~5.5 km) → high hit-rate, still local

function q(x) { return (Math.round(x / Q) * Q).toFixed(2); }   // snap lat/lon to a grid

export default {
  async fetch(req, env, ctx) {
    // (optional) shared-secret gate so this isn't an open Waze relay — see §11
    const url = new URL(req.url);
    const lat = parseFloat(url.searchParams.get("lat"));
    const lon = parseFloat(url.searchParams.get("lon"));
    if (!isFinite(lat) || !isFinite(lon)) return new Response("bad coords", { status: 400 });

    const bucket   = Math.floor(Date.now() / 1000 / TTL_S);     // time bucket → auto-expiry per window
    const cacheKey = new Request(`https://waze.cache/${q(lat)},${q(lon)},${bucket}`, req);
    const cache    = caches.default;
    const hit = await cache.match(cacheKey);
    if (hit) return hit;                                        // HIT: zero upstream calls (fleet dedup)

    // MISS: one upstream call, using the quantized cell's bbox
    const clat = parseFloat(q(lat)), clon = parseFloat(q(lon));
    const params = new URLSearchParams({
      "bottom-left": `${clat - BBOX_DEG},${clon - BBOX_DEG}`,
      "top-right":   `${clat + BBOX_DEG},${clon + BBOX_DEG}`,
    });
    let raw;
    try {
      const r = await fetch(`${WAZE_URL}?${params}`, {
        headers: { "x-rapidapi-host": WAZE_HOST, "x-rapidapi-key": env.WAZE_KEY },
        cf: { cacheTtl: 0 },
      });
      raw = await r.json();
    } catch (e) {                                              // upstream error → empty, never a 5xx
      return new Response(JSON.stringify({ generated_at: Math.floor(Date.now()/1000), alerts: [] }),
                          { status: 200, headers: { "content-type": "application/json" } });
    }

    const src = Array.isArray(raw) ? raw : (raw.alerts || []);
    const alerts = src.filter(a => a && a.type === "POLICE").map(a => ({
      lat: a.locationY, lon: a.locationX, magvar: a.magvar, ts: a.timestamp,
      uuid: a.uuid || a.id, street: a.street || "", town: a.city || "",
    }));
    const body = JSON.stringify({ generated_at: Math.floor(Date.now()/1000), ttl_s: TTL_S, alerts });
    const resp = new Response(body, { headers: {
      "content-type": "application/json",
      "cache-control": `public, max-age=${TTL_S}`,             // drives cache.put expiry
    }});
    ctx.waitUntil(cache.put(cacheKey, resp.clone()));
    return resp;
  },
};
```

Deploy with `wrangler` (`wrangler secret put WAZE_KEY`, `wrangler deploy`); route it to
`waze.<domain>/alerts`. No repo secrets on any device, no AWS creds, no cron.

### 3.2 Free-tier headroom math

Worst case, every device polls every 60 s (`POLICE_POLL_S`). 100k req/day ÷ 86,400 s ≈ **~1.15 req/s
sustained** → **~69 device-polls/min**. At 1 poll/min/device that's **~69 concurrently-driving devices**
before touching the free ceiling — and those are *device* requests, not Waze calls. **Waze upstream calls
are far fewer**: only cache misses, i.e. ≈ (unique cells being driven) ÷ TTL. A handful of cars on I-5
share a few cells → a few Waze calls per TTL window, protecting the one shared quota. The Worker request
count is the loose limit; the Waze quota is the tight one, and the cache is exactly what shields it.

---

## 4. Alternative: nginx caching proxy — ONLY if you switch to the self-hosted container

**Reality check (corrected):** your live `connect` is a **pure static SPA on S3 + CloudFront** — `vite
build` → `aws s3 sync dist/ s3://comma-connect/` → CloudFront `E15SSGZKTAFZJK` (see
`comma-connect/CLAUDE.md`). **There is no nginx server running in production.** The `nginx.conf` exists
only *inside* the optional Docker image (`Dockerfile: FROM nginx:1.24`, run via `go/docker-compose.yml`),
and even there it's just a static file server (`try_files … /index.html`), **not** a reverse proxy. The
only always-on compute you run today is the upload **Lambda** (`comma-uploader-api`) — the very thing this
feature must avoid. **So this option does not apply to your current setup.**

It only becomes available if you *switch connect to the self-hosted nginx container* (`go/` compose)
instead of S3+CloudFront — then you could add a caching reverse-proxy block to *that* nginx. Given you're
on S3+CloudFront and already have Cloudflare/wrangler wired (§3), the Worker (§3) is the right choice;
this block is kept only for the container-deployment contingency:

```nginx
# in the existing server{} — a caching reverse proxy to the Waze upstream
proxy_cache_path /var/cache/nginx/waze levels=1:2 keys_zone=waze:10m
                 max_size=100m inactive=10m use_temp_path=off;

location /waze/alerts {
    # cache key = the rounded bbox the device asks for (quantize client-side or with a map{} block)
    proxy_cache            waze;
    proxy_cache_key        "$arg_bottom_left|$arg_top_right";
    proxy_cache_valid      200 180s;          # short TTL — police reports persist minutes
    proxy_cache_lock       on;                # collapse a miss-stampede into ONE upstream call (fleet dedup)
    proxy_cache_use_stale  error timeout updating;

    # inject the ONE shared key here; the device never sees it
    proxy_set_header       x-rapidapi-host  waze-api.p.rapidapi.com;
    proxy_set_header       x-rapidapi-key   "<SHARED_RAPIDAPI_KEY>";
    proxy_ssl_server_name  on;
    proxy_pass             https://waze-api.p.rapidapi.com/alerts$is_args$args;
}
```

`proxy_cache_lock on` is the nginx equivalent of the Worker's dedup: concurrent misses for the same key
collapse into a single upstream fetch. **Note:** nginx caches the *upstream* body verbatim, so either the
device keeps its existing parser (which already handles the raw Waze `locationX/Y`/`type` shape — the
simplest wiring), or you add a tiny transform. **Downsides vs the Worker:** it requires abandoning the
S3+CloudFront static deployment for the self-hosted container; the key + logic live on that one box; the
box must be reachable from the car over LTE (public HTTPS + an `API_HOST`-style config); it's a single
point, not edge-distributed; and you own its uptime. **Recommend the Worker (§3)** — you already run
S3+CloudFront + Cloudflare/wrangler, so it fits with no deployment change.

---

## 5. Response (transformed-alert) JSON schema

The proxy returns exactly what the device needs — the transform already done:

```jsonc
{
  "generated_at": 1751304000,     // epoch SECONDS the proxy produced/refreshed this (device staleness check)
  "ttl_s": 180,                   // the proxy's cache TTL (advisory; device applies its own ceiling too)
  "alerts": [
    {
      "type":   "POLICE",         // proxy already filtered to POLICE (kept for forward-compat/HAZARD later)
      "lat":    47.6011,          // == Waze locationY  → device raw-alert "lat"
      "lon":   -122.3128,         // == Waze locationX  → device raw-alert "lon"
      "magvar": 270,              // reporter heading   → device "magvar" → _police_dir()
      "ts":     1751303880000,    // Waze epoch-MILLIS  → device "ts" → _age_min()/POLICE_STALE_S
      "uuid":   "a1b2-…",         // Waze uuid|id       → device "uuid" (banner dedup)
      "street": "I-5 N",          // Waze street        → device "street"
      "town":   "Seattle"         // Waze city          → device "town"
    }
  ]
}
```

Field names match the device raw-alert dict (§1) so the client parser is a straight pass-through. Only
`generated_at`/`ttl_s` are added, for the device staleness gate (§6).

---

## 6. Freshness / graceful degradation

- **TTL** ~120-300 s (proposed **180 s**). Waze police reports are crowd-sourced and persist *minutes*;
  the device itself already keeps them up to `POLICE_STALE_S = 45 min`. A 2-5 min cache is operationally
  correct and shields the shared quota.
- **Device staleness gate:** ignore any response older than `max(ttl_s, POLICE_TILE_MAX_AGE_S)` (propose
  `POLICE_TILE_MAX_AGE_S = 1800`, 30 min) → treat as **empty**. The device's own `POLICE_STALE_S` still
  ages individual reports on top.
- **Graceful degradation preserved end-to-end:** a 404, a proxy error, a stale body, or no network all
  resolve to an **empty alert list** → the unchanged `_line_police()` yields `state:"clear"` (fresh but
  empty) or `state:"nodata"` (fetch failed) — **never a false positive**. The Worker itself returns
  `{alerts: []}` on an upstream error rather than a 5xx, so a Waze hiccup degrades to "clear", not a crash.

---

## 7. Device client change (`location_servicesd.py`)

Minimal, additive, reversible. **No geohash helper needed** (that was the rejected tile design, §10).

**Source selector.** Add a param (proposed `WazeSource`, values `"proxy"` | `"direct"`, default
`"direct"` for a behavior-neutral ship) read in `PoliceUpdater.run()`. Or piggyback the existing
`PROXY_CFG` override JSON by honoring `"source": "proxy"` + a `"proxy_url"` so no new param key /
`params_pyx.so` rebuild is needed for the first rollout.

**New fetch path** — a sibling to `_poll()` that GETs the proxy for the current position and returns the
**identical raw-alert list**, so `snapshot()`, `_line_police()`, `_age_min()`, `_police_dir()`, and the UI
are all unchanged:

```python
# constants near POLICE_POLL_S etc.
WAZE_PROXY_URL = "https://waze.<domain>/alerts"      # keyless edge proxy (Worker §3 or nginx §4)
POLICE_TILE_MAX_AGE_S = 30 * 60                       # ignore a response older than this → empty (§6)

def _poll_proxy(self, lat, lon):
    """proxy source: keyless GET of the edge proxy for the CURRENT position. Returns the SAME raw-alert
    shape _poll() returns. Any error / stale body → [] (graceful empty, never a false alert)."""
    q = urllib.parse.urlencode({"lat": lat, "lon": lon})   # or reuse bottom-left/top-right if proxy wants bbox
    req = urllib.request.Request(f"{WAZE_PROXY_URL}?{q}", headers={"User-Agent": "pnw-location/1.0"})
    with urllib.request.urlopen(req, timeout=POLICE_TIMEOUT_S) as r:
        data = json.loads(r.read())
    if _now_epoch() - float(data.get("generated_at", 0)) > POLICE_TILE_MAX_AGE_S:
        return []                                          # STALE → empty
    out = []
    for a in data.get("alerts", []):
        if a.get("type") != "POLICE":
            continue
        try:
            out.append({"lat": float(a["lat"]), "lon": float(a["lon"]),
                        "magvar": a.get("magvar"), "ts": a.get("ts"),
                        "uuid": a.get("uuid"), "street": a.get("street") or "", "town": a.get("town") or ""})
        except (KeyError, TypeError, ValueError):
            continue
    return out
```

**Wiring in `run()`** (the successful-poll branch, ~lines 271-276) becomes source-aware:

```python
alerts = self._poll_proxy(gps[0], gps[1]) if self._source == "proxy" \
         else self._poll(cfg, gps[0], gps[1])
```

Everything else — the isolated-thread HARD RULE, the `nodata`-on-failure invariant, backoff,
`LocationServicesEnabled` gating, freeway-only geometry — is unchanged. In `proxy` mode the device carries
**no key** and makes no RapidAPI call; `_load_cfg()`/`DEFAULT_PROXY` remain only for the `direct` fallback
and are deleted at the end of the migration (§12).

---

## 8. The scaling win (quantified)

| | Today (per-device direct) | Proposed (caching proxy) |
|---|---|---|
| Waze calls / TTL window | **N devices × poll rate** | **# unique cells driven** (independent of N) |
| Co-located devices | each queries its own bbox | **collapse to one cache key** |
| Key exposure | shipped in every device repo | one secret, in the proxy only |
| Cost of +1 device | +1× quota consumption | ~0 extra Waze calls (rides the cache) |
| Precompute / cron | n/a | **none** — lazy, only driven areas fetched |
| Device work | GET + key headers + bbox math | keyless GET + JSON parse |

---

## 9. S3's role here (optional write-through cache — marries both)

The literal "S3 serves it" property can still be honored **for the hot path**, without any precompute:

- **Write-through:** on a cache MISS, the proxy additionally writes the fresh transformed JSON to
  `s3://comma-connect/waze/{cell}.json`. Within the TTL, repeat reads for that cell can be served as **pure
  static S3 GETs** — "S3 serves it" for hot cells — while S3 remains a **cache, not the source of truth**
  (nothing is ever precomputed; a cell exists in S3 only *after* a car drove it and the proxy filled it).
- **Tradeoff:** adds a write path + a `generated_at` staleness field the device must honor, and only pays
  off if edge/proxy egress cost ever matters. The Worker's own edge cache already gives fleet dedup for
  free, so **treat this as optional** — ship without it, add it only if S3-served hot cells become
  worthwhile.
- If used, scope **public-read to the `waze/` prefix only** via a bucket-policy statement (`s3:GetObject`
  on `arn:aws:s3:::comma-connect/waze/*`); `drives/*` stays private. Never a bucket-wide public ACL.

---

## 10. Rejected alternative — static S3 tiles + GitHub Actions collector (on record)

The first draft of this doc precomputed **geohash-tiled** alert files
(`s3://comma-connect/waze/v1/{geohash}.json`, precision 5 ≈ 4.9 km) refreshed by a **scheduled GitHub
Actions collector** (`.github/workflows/`, cron `*/15`), with the device computing its own geohash and
GETing static tiles. **Rejected because:**

1. **Precompute-all is wasteful.** To guarantee a tile exists when a car arrives, you must refresh tiles
   the fleet may *never* drive — most of the planet's tiles are dead weight. Scoping to "recent drives +
   a PNW box" only narrows it; it's still precomputing on a guess.
2. **Cron / minutes / quota walls.** GH Actions scheduled runs floor at ~5 min, **jitter** (can be skipped
   under load), get **disabled after 60 days of repo inactivity**, and consume shared Actions minutes. The
   work scales with *tiles refreshed*, not *demand*.
3. **Static ≠ on-demand.** S3 can only return bytes that already exist; it cannot proxy / transform /
   query Waze at request time. So a pure-static design is *forced* into (1) or (2). The moment you accept a
   little request-time compute (the Worker), both problems vanish: fetch lazily, exactly when driven, and
   cache to dedup. That realization is the whole pivot.

Kept here so the reasoning is on record. The tile design also required two new on-device geohash helpers
in `geo.py`, which the proxy design **does not** need.

---

## 11. Tradeoffs & open questions

- **TTL vs freshness.** Longer TTL = fewer Waze calls but staler reports; 120-300 s balances it given
  reports persist minutes (§6). Tunable in one place (the proxy).
- **Cache-key quantization vs hit-rate.** Coarser rounding (`Q`) = more devices share a key = higher
  hit-rate + fewer Waze calls, but the returned bbox is centered on the *cell*, not the car. `Q≈0.05°`
  (~5.5 km) inside a `BBOX_DEG=0.30°` (~33 km) window keeps every real nearby report inside the box while
  maximizing sharing. Open question: tune `Q` against real fleet density.
- **Edge vs self-host.** Worker = no maintenance, edge-distributed, free-tier ceiling on *requests*;
  nginx = no new service but one box, its uptime, LTE reachability, and the key on that host. Recommend
  the Worker.
- **Auth — is the proxy an open Waze relay?** As sketched it's **open** — anyone who finds
  `waze.<domain>/alerts` can burn the shared quota. Mitigate with a lightweight gate: a shared bearer
  secret in a header (shipped like `DEFAULT_PROXY` is today, rotatable), a `Referer`/`User-Agent` check,
  or Cloudflare rate-limiting / a WAF rule per IP. **Open question:** shared-secret vs rate-limit-only — a
  shared secret in-distribution is only marginally better than today's shipped key, but it now guards
  *our quota-shield*, not the RapidAPI key itself. Recommend a rotatable shared header secret **plus**
  Cloudflare rate limiting.
- **Cost / limits.** Worker free tier 100k req/day (§3.2) — the loose limit; the Waze monthly quota is the
  tight one, and the cache is what protects it. Watch the Waze upstream call rate, not the Worker request
  rate.
- **Which query shape.** Device can send `?lat&lon` (proxy derives the bbox from the quantized cell — best
  for cache-sharing) or the legacy `bottom-left/top-right` bbox (simpler device diff, worse sharing since
  each device's box differs). Recommend `?lat&lon`.

---

## 12. Phased rollout / migration

1. **Stand up the proxy (no device change).** Deploy the Worker (§3) to `waze.<domain>` with the shared
   key as a `wrangler secret` + a rotatable request-gate secret + rate limiting. Verify a keyless
   `GET /alerts?lat&lon` returns the transformed schema and that repeat requests hit cache (Waze upstream
   call count stays low). Zero device impact.
2. **Add device `proxy` mode behind `WazeSource`, defaulting to legacy `direct`.** Deploy `_poll_proxy` +
   the selector (§7). Behavior-neutral ship (toggles default OFF/legacy). Validate on one device by setting
   `WazeSource=proxy`: confirm `police poll ok` heartbeats + real alerts, and **no RapidAPI traffic from
   the car**.
3. **Flip the default to `proxy`** distribution-wide once proven on a drive.
4. **Retire the per-device key.** Remove `DEFAULT_PROXY` and the `direct` branch (and the
   `/data/pnw/location/police_proxy.json` fallback) so no Waze key ever ships to a device again — the one
   key lives only in the proxy.

---

## 13. Files this proposal would add/touch (for the eventual implementer)

- `comma-connect/waze-proxy/` — the Cloudflare Worker (`wrangler.toml` + `src/index.js`, §3). NEW.
- **(contingency only, §4)** `comma-connect/nginx.conf` — add the caching `location /waze/alerts` block
  **only if** connect is moved off S3+CloudFront onto the self-hosted `go/` nginx container. Not
  applicable to the current static deployment.
- (optional §9) proxy write-through + a `waze/*` public-read bucket-policy statement on `comma-connect`.
- `pnw-pilot/system/location_services/location_servicesd.py` — add `_poll_proxy`, the `WazeSource`
  selector, and the staleness constant; keep legacy `_poll` / `DEFAULT_PROXY` as fallback until §12.4.
- **No change** to `selfdrive/ui/onroad/location_services_status.py` — the `police` block contract is
  unchanged. **No `geo.py` geohash helpers** (dropped with the tile design).
```
