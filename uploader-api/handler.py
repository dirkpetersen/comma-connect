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

Env: S3_BUCKET, ALLOWED_DONGLES, DEVICE_PUBKEYS + AUTH_SIG_MODE (device realm; see CLAUDE.md "Device JWT signature check"), AUTH0_DOMAIN + AUTH0_AUDIENCE (user realm;
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

def _load_device_pubkeys():
    """DEVICE_PUBKEYS env: JSON {dongle_id: {"n": b64url, "e": b64url}} (RSA public JWK members;
    make it from a PEM with tools/device_pubkey_env.py). Bad JSON is logged loudly, never silent."""
    raw = os.environ.get('DEVICE_PUBKEYS', '')
    if not raw:
        return {}
    try:
        keys = json.loads(raw)
        assert isinstance(keys, dict)
    except (ValueError, AssertionError) as e:
        print(f'AUTH_SIG_CONFIG_ERROR DEVICE_PUBKEYS unparseable: {type(e).__name__}')
        return {}
    good = {}
    for dongle, jwk in keys.items():
        # openpilot keys are RSA-2048+ with e=65537; anything else is a misconfiguration (or a
        # weak-key attack on the env var) -> loud error, and the dongle is treated as keyless.
        try:
            n = int.from_bytes(_b64url_decode(jwk['n']), 'big')
            e = int.from_bytes(_b64url_decode(jwk['e']), 'big')
            if e != 65537 or n.bit_length() < 2048:
                raise ValueError(f'e={e} n_bits={n.bit_length()}')
        except (KeyError, TypeError, ValueError, AttributeError) as ex:
            print(f'AUTH_SIG_CONFIG_ERROR DEVICE_PUBKEYS key for dongle={dongle} rejected: '
                  f'{type(ex).__name__}: {ex}')
            continue
        good[dongle] = jwk
    return good

DEVICE_PUBKEYS = _load_device_pubkeys()
# 'log' (default for the first deploy): a failed signature check is logged, request proceeds.
# 'enforce': a failed check returns 401. Anything else is treated as 'enforce' (fail closed).
AUTH_SIG_MODE = os.environ.get('AUTH_SIG_MODE', 'log')
JWT_SKEW_S = 300

def verify_device_jwt(token, dongle_id):
    """Verify an openpilot device JWT (common/api.py: RS256, /persist/comma/id_rsa, claims
    identity/nbf/iat/exp). Returns None if valid, else a short reason string. Never raises."""
    try:
        if not token or token.count('.') != 2:
            return 'malformed'
        h_b64, p_b64, s_b64 = token.split('.')
        header = json.loads(_b64url_decode(h_b64))
        if header.get('alg') != 'RS256':     # pinned: rejects none / HS256 / ES256 confusion
            return 'bad_alg'
        jwk = DEVICE_PUBKEYS.get(dongle_id)
        if not jwk:
            return 'nokey'
        if not _rs256_verify(f'{h_b64}.{p_b64}'.encode(), _b64url_decode(s_b64), jwk):
            return 'bad_sig'
        claims = json.loads(_b64url_decode(p_b64))
        if claims.get('identity') != dongle_id:
            return 'bad_identity'
        now = time.time()
        if 'exp' not in claims:
            return 'no_exp'
        if float(claims['exp']) < now - JWT_SKEW_S:
            return 'expired'
        if 'nbf' in claims and float(claims['nbf']) > now + JWT_SKEW_S:
            return 'not_yet_valid'
        return None
    except Exception as e:                    # any parse/type error is a verification failure
        return f'malformed:{type(e).__name__}'

def check_auth(event, dongle_id):
    if ALLOWED_DONGLES and dongle_id not in ALLOWED_DONGLES:
        return False
    token = bearer_token(event, 'JWT') or ''
    # identity pin (unchanged): the unverified claim must name the dongle in the URL
    if decode_jwt_payload(token).get('identity') != dongle_id:
        return False
    reason = verify_device_jwt(token, dongle_id)
    if reason is None:
        return True
    route = str(event.get('rawPath', '') or event.get('path', ''))[:80]
    route = re.sub(r'[^\x21-\x7e]', '', route)    # log-injection safe; the token is never logged
    enforcing = AUTH_SIG_MODE != 'log'
    tag = 'AUTH_SIG_NOKEY' if reason == 'nokey' else 'AUTH_SIG_FAIL'
    print(f'{tag} reason={reason} dongle={dongle_id} route={route} '
          f'mode={AUTH_SIG_MODE} action={"reject" if enforcing else "allow"}')
    return not enforcing

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

# ---------------------------------------------------------------- CURVEDB routes (device JWT, ALWAYS enforced)
# They hand out private data, so unlike check_auth they ignore AUTH_SIG_MODE (no log mode).

CURVEDB_BUCKET = os.environ.get('CURVEDB_BUCKET', 'comma-curvedb-backup-454885954148')
CURVEDB_DONGLES = set(d for d in os.environ.get('CURVEDB_DONGLES', '').split(',') if d)
CURVEDB_PRESIGN_S = 300
CURVEDB_REPORT_MAX = 2048
HEX64_RE = re.compile(r'^[0-9a-f]{64}$')
REPORT_STATES = {'disabled', 'waiting_clock', 'waiting_unmetered', 'none_published', 'current',
                 'installed', 'held_hand_change', 'refused', 'error'}
REPORT_SHAS = ('live_sha', 'pointer_sha', 'last_auto_sha')
REPORT_KEYS = set(REPORT_SHAS) | {'auto_utc', 'loaded', 'state', 'reason', 'free_mb', 'git_sha'}

def curvedb_auth(event, dongle_id):
    """True only for a listed dongle with a fully verified device JWT. Never logs the token."""
    if dongle_id not in CURVEDB_DONGLES:
        print(f'CURVEDB_AUTH_FAIL reason=not_listed dongle={dongle_id}')
        return False
    reason = verify_device_jwt(bearer_token(event, 'JWT') or '', dongle_id)
    if reason is not None:
        print(f'CURVEDB_AUTH_FAIL reason={reason} dongle={dongle_id}')
        return False
    return True

def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0

def handle_curvedb_approved(dongle_id):
    try:
        ptr = json.loads(s3().get_object(Bucket=CURVEDB_BUCKET, Key='curvedb/approved.json')['Body'].read())
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') in ('NoSuchKey', '404'):
            return {'statusCode': 404, 'headers': {'Content-Type': 'application/json'},
                    'body': json.dumps({'error': 'none published'})}
        print(f'CURVEDB_S3_ERROR code={e.response.get("Error", {}).get("Code")} dongle={dongle_id}')
        return err(503, 'temporarily unavailable')
    except Exception as e:                    # network / unparseable pointer: fail closed, never "none"
        print(f'CURVEDB_POINTER_ERROR {type(e).__name__} dongle={dongle_id}')
        return err(503, 'temporarily unavailable')
    if not (isinstance(ptr, dict) and isinstance(ptr.get('sha256'), str) and HEX64_RE.match(ptr['sha256'])
            and isinstance(ptr.get('manifest_sha256'), str) and HEX64_RE.match(ptr['manifest_sha256'])
            and _is_int(ptr.get('rows_bytes')) and _is_int(ptr.get('manifest_bytes'))
            and isinstance(ptr.get('build'), str) and isinstance(ptr.get('approved_utc'), str)):
        print(f'CURVEDB_POINTER_MALFORMED dongle={dongle_id}')
        return err(503, 'temporarily unavailable')
    sha = ptr['sha256']
    try:     # keys come from the validated sha, never from pointer-supplied key strings
        urls = {name: s3().generate_presigned_url(
                    'get_object', Params={'Bucket': CURVEDB_BUCKET, 'Key': f'curvedb/tables/{sha}/{fn}'},
                    ExpiresIn=CURVEDB_PRESIGN_S)
                for name, fn in (('rows_url', 'curvedb_v2_rows.json.zst'), ('manifest_url', 'manifest.json'))}
    except Exception as e:
        print(f'CURVEDB_PRESIGN_ERROR {type(e).__name__} dongle={dongle_id}')
        return err(503, 'temporarily unavailable')
    out = {k: ptr[k] for k in ('sha256', 'rows_bytes', 'manifest_sha256', 'manifest_bytes', 'build', 'approved_utc')}
    out.update(urls)
    print(f'CURVEDB_APPROVED dongle={dongle_id} sha={sha[:8]}')    # URLs never logged
    return ok(out)

def handle_curvedb_report(event, dongle_id):
    raw = event.get('body') or ''
    if event.get('isBase64Encoded'):
        try:
            raw = base64.b64decode(raw).decode()
        except Exception:
            return err(400, 'bad body')
    if len(raw.encode()) > CURVEDB_REPORT_MAX:
        return err(400, 'body too large')
    try:
        body = json.loads(raw)
    except ValueError:
        return err(400, 'bad body')
    if not isinstance(body, dict) or set(body) - REPORT_KEYS:
        return err(400, 'bad schema')
    rec = {}
    for k, v in body.items():
        if k in REPORT_SHAS:
            if v is not None and not (isinstance(v, str) and HEX64_RE.match(v)):
                return err(400, f'bad {k}')
        elif k == 'state':
            if v not in REPORT_STATES:
                return err(400, 'bad state')
        elif k == 'reason':
            if not isinstance(v, str) or len(v) > 200:
                return err(400, 'bad reason')
            v = re.sub(r'[^\x20-\x7e]', '?', v)
        elif k == 'loaded':
            if not isinstance(v, bool):
                return err(400, 'bad loaded')
        elif k == 'free_mb':
            if not _is_int(v):
                return err(400, 'bad free_mb')
        elif not (isinstance(v, str) and len(v) <= 64 and re.match(r'^[A-Za-z0-9:._+-]*$', v)):
            return err(400, f'bad {k}')     # auto_utc, git_sha
        rec[k] = v
    rec['received_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    try:
        s3().put_object(Bucket=CURVEDB_BUCKET, Key=f'curvedb/device/{dongle_id}/report.json',
                        Body=json.dumps(rec).encode(), ContentType='application/json')
    except Exception as e:
        print(f'CURVEDB_REPORT_PUT_ERROR {type(e).__name__} dongle={dongle_id}')
        return err(503, 'temporarily unavailable')
    live = rec.get('live_sha') or ''
    print(f'CURVEDB_REPORT dongle={dongle_id} state={rec.get("state")} live={live[:8]}')
    return ok({'ok': True})

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

    # curve DB pull (device JWT, signature ALWAYS enforced, see curvedb_auth)
    m = re.match(r'^/v1/curvedb/([0-9a-f]{16})/(approved|report)/?$', path)
    if m and method == ('GET' if m.group(2) == 'approved' else 'POST'):
        dongle_id = m.group(1)
        if not curvedb_auth(event, dongle_id):
            return err(401, 'unauthorized')
        if m.group(2) == 'approved':
            return handle_curvedb_approved(dongle_id)
        return handle_curvedb_report(event, dongle_id)

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
