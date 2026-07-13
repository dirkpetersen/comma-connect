// waze-proxy — fleet-scale Waze police alerts via an on-demand caching proxy.
// Design: comma-connect/WAZE-API.md. Devices make a KEYLESS GET /alerts?lat=..&lon=..;
// the one shared RapidAPI key lives here (wrangler secret WAZE_KEY) and is hit only on
// cache misses. Cache = Workers KV keyed on a quantized lat/lon cell, so every device on
// the same stretch of road shares one upstream call per TTL window.
//
// Response contract (device: PoliceUpdater._poll_proxy in location_servicesd.py):
//   { generated_at: <epoch s>, ttl_s: <int>, alerts: [ {type, lat, lon, magvar, ts,
//     uuid, street, town} ], error?: "<short tag>" }
// - alert fields mirror the device's raw-alert dict (lat=Waze locationY, lon=locationX,
//   ts=Waze epoch-MILLIS). The device does all geometry/staleness itself.
// - upstream failure => HTTP 200 with alerts:[] AND an `error` tag; the device maps any
//   `error` to state "nodata" (never a false "clear"). Errors are never cached.

const WAZE_URL = "https://waze-api.p.rapidapi.com/alerts";
const WAZE_HOST = "waze-api.p.rapidapi.com";
const TTL_S = 180;      // police reports persist minutes; 2-5 min shields the shared quota
const BBOX_DEG = 0.30;  // must match the device's POLICE_BBOX_DEG (~±20 mi)
const Q = 0.05;         // cache-cell size ~5.5 km: shared enough to dedup, local enough to matter

// In-isolate L1 on top of KV: free hits while the isolate stays warm.
const l1 = new Map(); // cell -> { exp: epoch_ms, body: string }

function quant(x) {
  return (Math.round(x / Q) * Q).toFixed(2);
}

function json200(body) {
  return new Response(body, { headers: { "content-type": "application/json" } });
}

async function fetchWaze(env, clat, clon) {
  // Widen by Q/2: the cache cell's center can sit up to half a cell from the requesting car,
  // so the upstream box must over-cover to keep the device's full ±BBOX_DEG view populated.
  const half = BBOX_DEG + Q / 2;
  const params = new URLSearchParams({
    "bottom-left": `${(clat - half).toFixed(4)},${(clon - half).toFixed(4)}`,
    "top-right": `${(clat + half).toFixed(4)},${(clon + half).toFixed(4)}`,
  });
  const r = await fetch(`${WAZE_URL}?${params}`, {
    headers: { "x-rapidapi-host": WAZE_HOST, "x-rapidapi-key": env.WAZE_KEY },
  });
  if (!r.ok) throw new Error(`upstream ${r.status}`);
  const raw = await r.json();
  const src = Array.isArray(raw) ? raw : raw && Array.isArray(raw.alerts) ? raw.alerts : [];
  return src
    .filter((a) => a && a.type === "POLICE")
    .map((a) => ({
      type: "POLICE",
      lat: a.locationY,
      lon: a.locationX,
      magvar: a.magvar ?? null,
      ts: a.timestamp ?? null,
      uuid: a.uuid || a.id || null,
      street: a.street || "",
      town: a.city || "",
    }));
}

export default {
  async fetch(req, env, ctx) {
    const url = new URL(req.url);
    if (req.method !== "GET" || url.pathname !== "/alerts") {
      return new Response("not found", { status: 404 });
    }
    // Rotatable shared-secret gate (guards OUR quota shield, not the RapidAPI key).
    // Unset PROXY_SECRET = open proxy. Rotate with `wrangler secret put PROXY_SECRET`.
    if (env.PROXY_SECRET && req.headers.get("x-pnw-auth") !== env.PROXY_SECRET) {
      return new Response("unauthorized", { status: 401 });
    }
    const lat = parseFloat(url.searchParams.get("lat"));
    const lon = parseFloat(url.searchParams.get("lon"));
    if (!isFinite(lat) || !isFinite(lon) || Math.abs(lat) > 90 || Math.abs(lon) > 180) {
      return new Response("bad coords", { status: 400 });
    }

    const cell = `v1:${quant(lat)},${quant(lon)}`;
    const now = Date.now();

    const warm = l1.get(cell);
    if (warm && warm.exp > now) {
      const resp = json200(warm.body);
      resp.headers.set("x-waze-cache", "HIT-L1");
      return resp;
    }

    const kvHit = await env.WAZE_CACHE.get(cell);
    if (kvHit !== null) {
      // Known minor: seeding L1 from a KV hit can stretch total cache life to ~2×TTL (≤6 min);
      // harmless — the device ages each alert by its own Waze timestamp anyway.
      l1.set(cell, { exp: now + TTL_S * 1000, body: kvHit });
      const resp = json200(kvHit);
      resp.headers.set("x-waze-cache", "HIT");
      return resp;
    }

    // MISS: one upstream call for the whole fleet's cell.
    let alerts, errTag;
    try {
      alerts = await fetchWaze(env, parseFloat(quant(lat)), parseFloat(quant(lon)));
    } catch (e) {
      alerts = [];
      errTag = String(e && e.message ? e.message : "upstream err").slice(0, 40);
    }
    const body = JSON.stringify({
      generated_at: Math.floor(now / 1000),
      ttl_s: TTL_S,
      alerts,
      ...(errTag ? { error: errTag } : {}),
    });
    if (!errTag) {
      // Never cache errors — the next poll retries upstream.
      l1.set(cell, { exp: now + TTL_S * 1000, body });
      ctx.waitUntil(env.WAZE_CACHE.put(cell, body, { expirationTtl: TTL_S }));
    }
    const resp = json200(body);
    resp.headers.set("x-waze-cache", errTag ? "MISS-ERR" : "MISS");
    return resp;
  },
};
