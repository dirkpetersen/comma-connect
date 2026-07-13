"""comma-waze-proxy Lambda — fleet-scale Waze police alerts, keyless for devices.

Design: comma-connect/WAZE-API.md (AWS variant of the Cloudflare-Worker design). Devices
GET /alerts?lat=..&lon=.. with NO key; this function holds the one shared RapidAPI key
(env WAZE_KEY) and calls Waze only on a cache miss. Cache = DynamoDB (TTL-expiring) keyed on
a quantized ~5.5 km cell, plus an in-isolate L1 dict, so the whole fleet dedups onto one
upstream call per cell per TTL window — independent of device count.

COST/RUNTIME (owner concern 2026-07-13): this runs for tens of ms, not "a long time".
  - cache HIT  -> one DynamoDB read, ~10-30 ms, no upstream call
  - cache MISS -> one Waze call, ~200-400 ms  (rare: 1 per cell per TTL across the fleet)
  Function timeout is capped at 5 s so a hung upstream can never bill more than that; the
  urllib timeout is 4 s. No sleeps, no polling, no long-lived work.

Response contract (device: PoliceUpdater._parse_proxy_body):
  { generated_at:<epoch s>, ttl_s:<int>, alerts:[{type,lat,lon,magvar,ts,uuid,street,town}],
    error?:"<tag>" }  -- upstream failure => 200 + alerts:[] + error tag (device -> nodata,
  never a false clear); errors are never cached.
"""
import json
import os
import time
import urllib.parse
import urllib.request

import boto3

WAZE_URL = 'https://waze-api.p.rapidapi.com/alerts'
WAZE_HOST = 'waze-api.p.rapidapi.com'
WAZE_KEY = os.environ.get('WAZE_KEY', '')
CACHE_TABLE = os.environ.get('WAZE_TABLE', 'comma-waze-cache')
PROXY_SECRET = os.environ.get('PROXY_SECRET', '')      # optional x-pnw-auth gate; '' = open

TTL_S = 180        # police reports persist minutes; 2-5 min shields the shared quota
BBOX_DEG = 0.30    # match the device's POLICE_BBOX_DEG (~±20 mi)
Q = 0.05           # cache-cell size ~5.5 km
UPSTREAM_TIMEOUT_S = 4

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


def _fetch_waze(clat, clon):
    # widen by Q/2: the cell center can sit half a cell from the car, so over-cover the box
    half = BBOX_DEG + Q / 2
    params = urllib.parse.urlencode({
        'bottom-left': f'{clat - half:.4f},{clon - half:.4f}',
        'top-right': f'{clat + half:.4f},{clon + half:.4f}',
    })
    req = urllib.request.Request(f'{WAZE_URL}?{params}',
                                 headers={'x-rapidapi-host': WAZE_HOST, 'x-rapidapi-key': WAZE_KEY})
    with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT_S) as r:
        raw = json.loads(r.read())
    src = raw if isinstance(raw, list) else (raw.get('alerts', []) if isinstance(raw, dict) else [])
    out = []
    for a in src:
        if not isinstance(a, dict) or a.get('type') != 'POLICE':
            continue
        out.append({'type': 'POLICE', 'lat': a.get('locationY'), 'lon': a.get('locationX'),
                    'magvar': a.get('magvar'), 'ts': a.get('timestamp'),
                    'uuid': a.get('uuid') or a.get('id'), 'street': a.get('street') or '',
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

    # MISS: one upstream call for the whole fleet's cell
    err_tag = None
    try:
        alerts = _fetch_waze(float(_q(lat)), float(_q(lon)))
    except Exception as e:
        alerts, err_tag = [], f'upstream {type(e).__name__}'[:40]

    body = json.dumps({'generated_at': now, 'ttl_s': TTL_S, 'alerts': alerts,
                       **({'error': err_tag} if err_tag else {})})
    if not err_tag:                                          # never cache errors -> next poll retries
        _l1[cell] = (now + TTL_S, body)
        try:
            table().put_item(Item={'cell': cell, 'body': body, 'exp': now + TTL_S})
        except Exception as e:
            print(f'cache_write_error {type(e).__name__}: {e}')
    return _resp(200, body)
