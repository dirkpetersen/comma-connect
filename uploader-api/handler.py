"""comma-uploader-api Lambda — the self-hosted connect backend.

Two independent auth realms share this function:
  DEVICE  "Authorization: JWT <token>"    — openpilot's own device JWT (identity == dongle_id).
          Serves upload_url / device record / device /v1/me. Unchanged since day 1.
  USER    "Authorization: Bearer <token>" — Auth0 RS256 access token (Google/GitHub/LinkedIn
          login from the connect SPA), verified against the tenant JWKS (signature + iss +
          aud + exp) in pure stdlib (no Lambda layers). Serves the user profile + the
          device-claim flow.

Device-claim model (owner decision 2026-07-12): a fresh pnw-pilot install starts uploading
before anyone owns it. A logged-in user may claim a dongle_id iff (a) that device has actually
uploaded to our bucket and (b) nobody has claimed it yet — first-claim-wins. The list of
unclaimed devices is NEVER exposed; a claimant must arrive knowing their device's ID (from the
device UI). State lives in S3: users/{sub}.json and claims/{dongle_id}.json.

Env: S3_BUCKET, ALLOWED_DONGLES (device realm), AUTH0_DOMAIN + AUTH0_AUDIENCE (user realm;
empty = user realm disabled), ALLOWED_ORIGINS (CORS, comma-separated).
"""
import json
import os
import re
import base64
import hashlib
import hmac
import time
import urllib.request
import boto3
from botocore.exceptions import ClientError

BUCKET = os.environ['S3_BUCKET']
ALLOWED_DONGLES = set(d for d in os.environ.get('ALLOWED_DONGLES', '').split(',') if d)
REGION = os.environ.get('AWS_REGION', 'us-west-2')
AUTH0_DOMAIN = os.environ.get('AUTH0_DOMAIN', '')            # e.g. dev-xyz.us.auth0.com
AUTH0_AUDIENCE = os.environ.get('AUTH0_AUDIENCE', '')        # the Auth0 API identifier
ALLOWED_ORIGINS = set(o for o in os.environ.get(
    'ALLOWED_ORIGINS',
    'https://comma-connect.aws.internetchen.de,http://localhost:3000').split(',') if o)

DONGLE_RE = re.compile(r'^[0-9a-f]{16}$')

_s3 = None
def s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client('s3', region_name=REGION)
    return _s3

# ---------------------------------------------------------------- shared helpers

def _b64url_decode(s):
    if isinstance(s, str):
        s = s.encode()
    return base64.urlsafe_b64decode(s + b'=' * (-len(s) % 4))

def decode_jwt_payload(token):
    try:
        return json.loads(_b64url_decode(token.split('.')[1]))
    except Exception:
        return {}

def cors_headers(event):
    origin = (event.get('headers') or {}).get('origin', '')
    if origin in ALLOWED_ORIGINS:
        return {'Access-Control-Allow-Origin': origin,
                'Access-Control-Allow-Headers': 'authorization,content-type',
                'Access-Control-Allow-Methods': 'GET,POST,OPTIONS'}
    return {}

def ok(body, event=None):
    h = {'Content-Type': 'application/json'}
    if event is not None:
        h.update(cors_headers(event))
    return {'statusCode': 200, 'headers': h, 'body': json.dumps(body)}

def err(code, msg, event=None):
    h = cors_headers(event) if event is not None else {}
    return {'statusCode': code, 'headers': h, 'body': msg}

def bearer_token(event, prefix):
    headers = event.get('headers') or {}
    auth = headers.get('authorization', '') or headers.get('Authorization', '')
    if not auth.startswith(prefix + ' '):
        return None
    return auth[len(prefix) + 1:].strip()

# ---------------------------------------------------------------- DEVICE realm (JWT <token>)

def check_auth(event, dongle_id):
    if ALLOWED_DONGLES and dongle_id not in ALLOWED_DONGLES:
        return False
    token = bearer_token(event, 'JWT') or ''
    # device JWTs are decoded without signature verification (private API; identity pinning +
    # the dongle allowlist bound the blast radius to "can upload files into its own prefix")
    payload = decode_jwt_payload(token)
    return payload.get('identity') == dongle_id

def handle_upload_url(event, dongle_id):
    query = event.get('queryStringParameters') or {}
    file_path = query.get('path', '')
    if not file_path:
        return err(400, 'missing path')
    # sanitize: the path is interpolated into the S3 key — no traversal, no absolute paths
    if '..' in file_path or file_path.startswith('/') or '\\' in file_path:
        return err(400, 'bad path')
    url = s3().generate_presigned_url(
        'put_object',
        Params={'Bucket': BUCKET, 'Key': f'drives/{dongle_id}/{file_path}'},
        ExpiresIn=3600,
    )
    return ok({'url': url, 'headers': {}})

def handle_device(dongle_id):
    return ok({
        'dongle_id': dongle_id,
        'alias': '',
        'serial': '',
        'athena_host': '',
        'eligible_features': {},
        'is_owner': True,
    })

# ---------------------------------------------------------------- USER realm (Bearer <token>, Auth0)

_jwks_cache = {'keys': None, 'at': 0.0}
JWKS_TTL_S = 6 * 3600
# EMSA-PKCS1-v1_5 DigestInfo prefix for SHA-256
_SHA256_DIGESTINFO = bytes.fromhex('3031300d060960864801650304020105000420')

def _jwks(force=False):
    now = time.monotonic()
    if force or _jwks_cache['keys'] is None or now - _jwks_cache['at'] > JWKS_TTL_S:
        with urllib.request.urlopen(f'https://{AUTH0_DOMAIN}/.well-known/jwks.json', timeout=5) as r:
            _jwks_cache['keys'] = {k['kid']: k for k in json.loads(r.read())['keys']}
            _jwks_cache['at'] = now
    return _jwks_cache['keys']

def _rs256_verify(signing_input: bytes, sig: bytes, jwk) -> bool:
    n = int.from_bytes(_b64url_decode(jwk['n']), 'big')
    e = int.from_bytes(_b64url_decode(jwk['e']), 'big')
    k = (n.bit_length() + 7) // 8
    if len(sig) != k:
        return False
    sig_int = int.from_bytes(sig, 'big')
    if sig_int >= n:                              # reject non-canonical s >= n (malleability)
        return False
    em = pow(sig_int, e, n).to_bytes(k, 'big')
    h = hashlib.sha256(signing_input).digest()
    pad_len = k - len(_SHA256_DIGESTINFO) - len(h) - 3
    if pad_len < 8:
        return False
    expected = b'\x00\x01' + b'\xff' * pad_len + b'\x00' + _SHA256_DIGESTINFO + h
    return hmac.compare_digest(em, expected)

def verify_auth0(event):
    """Returns the verified Auth0 claims dict, or None. Full verification: RS256 signature
    against the tenant JWKS + iss + aud + exp."""
    if not (AUTH0_DOMAIN and AUTH0_AUDIENCE):
        return None
    token = bearer_token(event, 'Bearer')
    if not token or token.count('.') != 2:
        return None
    try:
        h_b64, p_b64, s_b64 = token.split('.')
        header = json.loads(_b64url_decode(h_b64))
        if header.get('alg') != 'RS256':
            return None
        kid = header.get('kid', '')
        jwk = _jwks().get(kid)
        if jwk is None:
            jwk = _jwks(force=True).get(kid)     # key rotation: refresh the cache once
        if jwk is None:
            return None
        if not _rs256_verify(f'{h_b64}.{p_b64}'.encode(), _b64url_decode(s_b64), jwk):
            return None
        claims = json.loads(_b64url_decode(p_b64))
        if claims.get('iss') != f'https://{AUTH0_DOMAIN}/':
            return None
        aud = claims.get('aud')
        if AUTH0_AUDIENCE not in (aud if isinstance(aud, list) else [aud]):
            return None
        if float(claims.get('exp', 0)) < time.time():
            return None
        return claims
    except Exception:
        return None

def _s3_json(key):
    """Parsed JSON, or None ONLY if the key genuinely does not exist. Any other S3 error
    (transient 500/503, throttle, network) RAISES — never fail open: a blip must not read as
    'device unclaimed' (would let a claimed device be stolen) or 'user has no dongles' (would
    wipe the profile on the next write)."""
    try:
        return json.loads(s3().get_object(Bucket=BUCKET, Key=key)['Body'].read())
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') in ('NoSuchKey', '404'):
            return None
        raise

def _get_user(sub):
    return _s3_json(f'users/{_safe_sub(sub)}.json') or {'sub': sub, 'dongles': []}

def _safe_sub(sub):
    # Auth0 subs look like "google-oauth2|1234567890" — make S3-key-safe
    return re.sub(r'[^A-Za-z0-9_.-]', '_', sub)[:128]

def handle_user_me(event, claims):
    user = _get_user(claims['sub'])
    return ok({'email': claims.get('email', user.get('email', '')), 'id': claims['sub'],
               'dongles': user.get('dongles', []), 'superuser': False}, event)

def handle_claim(event, claims):
    raw = event.get('body') or '{}'
    if event.get('isBase64Encoded'):                 # API Gateway may base64 the body — decode FIRST
        try:
            raw = base64.b64decode(raw).decode()
        except Exception:
            return err(400, 'bad body', event)
    try:
        body = json.loads(raw)
    except Exception:
        return err(400, 'bad body', event)
    dongle_id = str(body.get('dongle_id', '')).strip().lower()
    if not DONGLE_RE.match(dongle_id):
        return err(400, 'bad dongle_id', event)

    claim_key = f'claims/{dongle_id}.json'
    # Run BOTH checks unconditionally so response timing can't distinguish "no such device" from
    # "already claimed" (enumeration guard). Any S3 error fails CLOSED (503), never fail-open.
    try:
        uploaded = s3().list_objects_v2(
            Bucket=BUCKET, Prefix=f'drives/{dongle_id}/', MaxKeys=1).get('KeyCount', 0) > 0
        existing = _s3_json(claim_key)               # (b) unclaimed? — raises on transient error
    except Exception:
        return err(503, 'temporarily unavailable', event)

    if existing is not None and existing.get('sub') == claims['sub']:
        return ok({'dongle_id': dongle_id, 'claimed': True, 'already': True}, event)
    # (a) must have uploaded AND (b) must be unclaimed — one indistinguishable 403 for either miss
    if not uploaded or existing is not None:
        return err(403, 'device not claimable', event)

    record = {'sub': claims['sub'], 'email': claims.get('email', ''), 'ts': int(time.time())}
    # Atomic first-claim-wins: IfNoneMatch='*' makes the PUT fail if a racing claim landed first.
    try:
        s3().put_object(Bucket=BUCKET, Key=claim_key, Body=json.dumps(record).encode(),
                        ContentType='application/json', IfNoneMatch='*')
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') in ('PreconditionFailed', '412'):
            return err(403, 'device not claimable', event)   # lost the race
        return err(503, 'temporarily unavailable', event)

    # Update the user profile (read-modify-write). _s3_json now RAISES on a transient read error,
    # so a blip can't silently wipe existing dongles — we just skip the profile update (the claim
    # record is authoritative; a re-claim is idempotent and repairs the profile).
    try:
        user = _get_user(claims['sub'])
        user['email'] = claims.get('email', user.get('email', ''))
        if dongle_id not in user['dongles']:
            user['dongles'].append(dongle_id)
        s3().put_object(Bucket=BUCKET, Key=f'users/{_safe_sub(claims["sub"])}.json',
                        Body=json.dumps(user).encode(), ContentType='application/json')
    except Exception:
        print(f'CLAIM_PROFILE_WARN sub={claims["sub"]} dongle={dongle_id} (claim ok, profile skipped)')
    print(f'CLAIM sub={claims["sub"]} email={claims.get("email", "")} dongle={dongle_id}')
    return ok({'dongle_id': dongle_id, 'claimed': True}, event)

# ---------------------------------------------------------------- dispatch

def handler(event, context):
    path = event.get('rawPath', '') or event.get('path', '')
    http_ctx = event.get('requestContext', {}).get('http', {})
    method = http_ctx.get('method', 'GET')
    src_ip = http_ctx.get('sourceIp', '')
    # local_ip: self-reported LAN address from the device's uploader (query param) -- AWS/NAT can
    # never see the private address, so the device tells us. Used to locate the roaming comma.
    # sanitized: unauthenticated callers reach this print; strip CR/LF (log injection) + cap length
    local_ip = (event.get('queryStringParameters') or {}).get('local_ip', '')[:45].replace('\n', '').replace('\r', '')
    print(f'CLIENT_IP src_ip={src_ip} local_ip={local_ip} method={method} path={path}')

    # CORS preflight (browser realm)
    if method == 'OPTIONS':
        return {'statusCode': 204, 'headers': cors_headers(event), 'body': ''}

    # ---- DEVICE realm --------------------------------------------------------
    # GET /v1.4/{dongle_id}/upload_url/
    m = re.match(r'^/v1\.4/([0-9a-f]{16})/upload_url/?$', path)
    if m and method == 'GET':
        dongle_id = m.group(1)
        if not check_auth(event, dongle_id):
            return err(401, 'Unauthorized')
        return handle_upload_url(event, dongle_id)

    # GET /v1.1/devices/{dongle_id} or /v1/devices/{dongle_id}
    m = re.match(r'^/v1(?:\.1)?/devices/([0-9a-f]{16})/?$', path)
    if m and method == 'GET':
        dongle_id = m.group(1)
        if not check_auth(event, dongle_id):
            return err(401, 'Unauthorized')
        return handle_device(dongle_id)

    # ---- USER realm (Auth0 Bearer) ------------------------------------------
    if path.rstrip('/') == '/v1/me' and method == 'GET':
        user_claims = verify_auth0(event)
        if user_claims is not None:
            return handle_user_me(event, user_claims)
        # fall through to the device-JWT /v1/me below

    if path.rstrip('/') == '/v1/claim' and method == 'POST':
        user_claims = verify_auth0(event)
        if user_claims is None:
            return err(401, 'Unauthorized', event)
        return handle_claim(event, user_claims)

    # GET /v1/me (device JWT variant — openpilot registration probe)
    if path.rstrip('/') == '/v1/me' and method == 'GET':
        token = bearer_token(event, 'JWT') or ''
        payload = decode_jwt_payload(token)
        dongle_id = payload.get('identity', '')
        if not dongle_id or (ALLOWED_DONGLES and dongle_id not in ALLOWED_DONGLES):
            return err(401, 'Unauthorized', event)   # pass event so a browser fall-through keeps CORS
        return ok({'email': '', 'id': dongle_id, 'points': 0, 'superuser': False})

    return err(404, f'Not found: {path}')
