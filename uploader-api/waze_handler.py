"""comma-waze-proxy Lambda — fleet-scale Waze police alerts, keyless for devices.

Design: comma-connect/WAZE-API.md (AWS variant of the Cloudflare-Worker design). Devices
GET /alerts?lat=..&lon=.. with NO key; this function holds the one shared OpenWebNinja PAYG
key (env WAZE_KEY) and calls the upstream Waze proxy only on a cache miss. Cache = DynamoDB
(TTL-expiring) keyed on a quantized ~5.5 km cell, plus an in-isolate L1 dict, so the whole
fleet dedups onto one upstream call per cell per TTL window — independent of device count.

UPSTREAM (switched 2026-08 from the RapidAPI `waze-api` listing to OpenWebNinja PAYG,
`https://api.openwebninja.com/waze/alerts-and-jams`, auth header `x-api-key`). Response shape
confirmed via a live test call (2026-08-13): alerts are at data['data']['alerts'], each a dict
with alert_id/type/latitude/longitude/publish_datetime_utc/street/city among other fields.
There is NO direction/bearing field in the OpenWebNinja payload, so the normalized `magvar` is
always None now — the device already tolerates a magvar-less report.

BUDGET CAPS: two independent caps, both enforced ONLY on cache-MISS upstream calls — cache
hits are free and never budget-checked.
  1. GLOBAL monthly: OpenWebNinja PAYG bills $0.005/upstream call; default $25/mo = 5000 calls
     (env WAZE_BUDGET_USD / WAZE_COST_PER_CALL). Counter item cell="budget:YYYY-MM" (UTC),
     attribute `n`. Exceeded -> HTTP 402 {"error":"budget exceeded", spent_usd, budget_usd}.
  2. PER-DEVICE daily: default 750 calls/device/day (env WAZE_DEVICE_DAILY), keyed on the
     `x-device-id` request header (comma serial, e.g. "eb1f2f7"; "noid" if the header is
     absent). Counter item cell="dev:{device_id}:YYYY-MM-DD" (UTC), attribute `n`. Exceeded
     -> HTTP 429 {"error":"daily limit","device":dev_id,"count":count,"limit":DEVICE_DAILY_LIMIT}.
Both counters live in the same DynamoDB table as the response cache. The 402 month boundary
and the 429 day boundary are both UTC. Neither check applies to a cache hit; on a genuine
cache-MISS the order is: (1) global budget, (2) per-device daily, (3) fetch — see handler().

COST/RUNTIME (owner concern 2026-07-13): this runs for tens of ms, not "a long time".
  - cache HIT  -> one DynamoDB read, ~10-30 ms, no upstream call, no budget check
  - cache MISS -> budget check, then one Waze call, ~200-400 ms (rare: 1 per cell per TTL
    across the fleet, further capped by the monthly budget above)
  Function timeout is capped at 5 s so a hung upstream can never bill more than that; the
  urllib timeout is 4 s. No sleeps, no polling, no long-lived work.

Response contract (device: PoliceUpdater._parse_proxy_body):
  { generated_at:<epoch s>, ttl_s:<int>, alerts:[{type,lat,lon,magvar,ts,uuid,street,town}],
    error?:"<tag>" }  -- upstream failure => 200 + alerts:[] + error tag (device -> nodata,
  never a false clear); errors are never cached. Budget-exceeded is a distinct 402, not this
  200 contract (see handler()).

NOTE: WAZE_KEY must be rotated to an OpenWebNinja key (the old RapidAPI key will not work
against this URL/header). The response shape above was confirmed against one live upstream
call before deploy; re-confirm with a fresh live call if OpenWebNinja changes their API.
"""
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import boto3

WAZE_URL = 'https://api.openwebninja.com/waze/alerts-and-jams'
WAZE_KEY = os.environ.get('WAZE_KEY', '')
CACHE_TABLE = os.environ.get('WAZE_TABLE', 'comma-waze-cache')
PROXY_SECRET = os.environ.get('PROXY_SECRET', '')      # optional x-pnw-auth gate; '' = open

TTL_S = 180        # police reports persist minutes; 2-5 min shields the shared quota
BBOX_DEG = 0.30    # match the device's POLICE_BBOX_DEG (~±20 mi)
Q = 0.05           # cache-cell size ~5.5 km
UPSTREAM_TIMEOUT_S = 4

# Monthly PAYG budget cap: OpenWebNinja bills COST_PER_CALL per upstream (cache-MISS) call.
BUDGET_USD = float(os.environ.get('WAZE_BUDGET_USD', '25'))
COST_PER_CALL = float(os.environ.get('WAZE_COST_PER_CALL', '0.005'))
BUDGET_CALLS = int(BUDGET_USD / COST_PER_CALL)

# Per-device daily cap, independent of (and checked after) the global monthly budget above.
DEVICE_DAILY_LIMIT = int(os.environ.get('WAZE_DEVICE_DAILY', '750'))

_ddb = None
def table():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource('dynamodb', region_name=os.environ.get('AWS_REGION', 'us-west-2')).Table(CACHE_TABLE)
    return _ddb

# in-isolate L1 on top of DynamoDB: free hits while the container stays warm
_l1 = {}   # cell -> (exp_epoch, body_str)


def _q(x):
    return f'{round(x / Q) * Q:.2f}'


def _resp(status, body, is_json=True):
    return {'statusCode': status,
            'headers': {'Content-Type': 'application/json' if is_json else 'text/plain'},
            'body': body}


def _iso_to_epoch_ms(s):
    """Parse an OpenWebNinja UTC timestamp like '2026-08-13T14:49:09.000Z' to epoch ms.

    Tolerates a trailing 'Z', fractional seconds, or their absence. Never raises — returns
    None on any parse failure so a malformed/missing timestamp degrades to a skippable alert
    rather than crashing the whole cache-miss path.
    """
    if not s:
        return None
    try:
        s = s.strip().replace('Z', '+00:00')
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def _budget_month_key():
    return f'budget:{datetime.now(timezone.utc):%Y-%m}'


def _budget_count():
    try:
        item = table().get_item(Key={'cell': _budget_month_key()}).get('Item')
        return int(item.get('n', 0)) if item else 0
    except Exception as e:
        print(f'budget_read_error {type(e).__name__}: {e}')   # best-effort; fail open
        return 0


def _budget_inc():
    try:
        table().update_item(Key={'cell': _budget_month_key()},
                            UpdateExpression='ADD n :one',
                            ExpressionAttributeValues={':one': 1})
    except Exception as e:
        print(f'budget_write_error {type(e).__name__}: {e}')   # best-effort; never block the response


def _device_day_key(dev_id):
    return f'dev:{dev_id}:{datetime.now(timezone.utc):%Y-%m-%d}'


def _device_count(dev_id):
    try:
        item = table().get_item(Key={'cell': _device_day_key(dev_id)}).get('Item')
        return int(item.get('n', 0)) if item else 0
    except Exception as e:
        print(f'device_read_error {type(e).__name__}: {e}')   # best-effort; fail open
        return 0


def _device_inc(dev_id):
    try:
        table().update_item(Key={'cell': _device_day_key(dev_id)},
                            UpdateExpression='ADD n :one',
                            ExpressionAttributeValues={':one': 1})
    except Exception as e:
        print(f'device_write_error {type(e).__name__}: {e}')   # best-effort; never block the response


def _fetch_waze(clat, clon):
    # widen by Q/2: the cell center can sit half a cell from the car, so over-cover the box
    half = BBOX_DEG + Q / 2
    params = urllib.parse.urlencode({
        'bottom_left': f'{clat - half:.4f},{clon - half:.4f}',
        'top_right': f'{clat + half:.4f},{clon + half:.4f}',
        'alert_types': 'POLICE',
        'max_jams': 0,
        'max_alerts': 20,
    })
    req = urllib.request.Request(f'{WAZE_URL}?{params}', headers={'x-api-key': WAZE_KEY})
    with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT_S) as r:
        raw = json.loads(r.read())
    src = raw.get('data', {}).get('alerts', []) if isinstance(raw, dict) else []
    if not isinstance(src, list):
        src = []
    out = []
    for a in src:
        if not isinstance(a, dict) or a.get('type') != 'POLICE':
            continue
        out.append({'type': 'POLICE', 'lat': a.get('latitude'), 'lon': a.get('longitude'),
                    'magvar': None, 'ts': _iso_to_epoch_ms(a.get('publish_datetime_utc')),
                    'uuid': a.get('alert_id'), 'street': a.get('street') or '',
                    'town': a.get('city') or ''})
    return out


def handler(event, context):
    method = event.get('requestContext', {}).get('http', {}).get('method', 'GET')
    path = event.get('rawPath', '') or event.get('path', '')
    if method != 'GET' or path.rstrip('/') != '/alerts':
        return _resp(404, 'not found', is_json=False)

    headers = event.get('headers') or {}
    if PROXY_SECRET and headers.get('x-pnw-auth', '') != PROXY_SECRET:
        return _resp(401, 'unauthorized', is_json=False)
    # API Gateway may pass headers in either case; normalize just for this lookup.
    dev_id = next((v for k, v in headers.items() if k.lower() == 'x-device-id'), None) or 'noid'

    q = event.get('queryStringParameters') or {}
    try:
        lat = float(q.get('lat', ''))
        lon = float(q.get('lon', ''))
    except (TypeError, ValueError):
        return _resp(400, 'bad coords', is_json=False)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return _resp(400, 'bad coords', is_json=False)

    cell = f'v1:{_q(lat)},{_q(lon)}'
    now = int(time.time())

    warm = _l1.get(cell)
    if warm and warm[0] > now:
        return _resp(200, warm[1])

    try:
        item = table().get_item(Key={'cell': cell}).get('Item')
        if item and int(item.get('exp', 0)) > now:
            body = item['body']
            _l1[cell] = (now + TTL_S, body)
            return _resp(200, body)
    except Exception as e:
        print(f'cache_read_error {type(e).__name__}: {e}')   # cache is best-effort; fall through to upstream

    # MISS: one upstream call for the whole fleet's cell -- gate on both caps before spending it.
    # Order: (1) global monthly budget, (2) per-device daily limit, (3) the actual fetch.
    spent_calls = _budget_count()
    if spent_calls >= BUDGET_CALLS:
        return _resp(402, json.dumps({'error': 'budget exceeded',
                                      'spent_usd': round(spent_calls * COST_PER_CALL, 2),
                                      'budget_usd': BUDGET_USD}))

    dev_calls = _device_count(dev_id)
    if dev_calls >= DEVICE_DAILY_LIMIT:
        return _resp(429, json.dumps({'error': 'daily limit', 'device': dev_id,
                                      'count': dev_calls, 'limit': DEVICE_DAILY_LIMIT}))

    err_tag = None
    try:
        alerts = _fetch_waze(float(_q(lat)), float(_q(lon)))
    except Exception as e:
        alerts, err_tag = [], f'upstream {type(e).__name__}'[:40]

    body = json.dumps({'generated_at': now, 'ttl_s': TTL_S, 'alerts': alerts,
                       **({'error': err_tag} if err_tag else {})})
    if not err_tag:                                          # never cache errors -> next poll retries
        _budget_inc()
        _device_inc(dev_id)
        _l1[cell] = (now + TTL_S, body)
        try:
            table().put_item(Item={'cell': cell, 'body': body, 'exp': now + TTL_S})
        except Exception as e:
            print(f'cache_write_error {type(e).__name__}: {e}')
    return _resp(200, body)
