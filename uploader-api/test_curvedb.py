"""Offline tests for the curve DB routes (GET approved / POST report). No AWS, no network.
Run: python3 -m pytest uploader-api/test_curvedb.py"""
import base64
import json
import os
import sys
import time
from unittest import mock

import pytest
from botocore.exceptions import ClientError

os.environ.setdefault('S3_BUCKET', 'test-bucket')
os.environ['AUTH0_DOMAIN'] = 'tenant.us.auth0.com'      # same as the other test files (first import wins)
os.environ['AUTH0_AUDIENCE'] = 'https://connect-api.test'
sys.path.insert(0, os.path.dirname(__file__))
import handler  # noqa: E402
from tools import device_pubkey_env  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa, padding  # noqa: E402
from cryptography.hazmat.primitives import hashes  # noqa: E402

DONGLE = '0123456789abcdef'
OTHER = 'fedcba9876543210'
SHA = 'ab' * 32
MSHA = 'cd' * 32
BUCKET = 'comma-curvedb-backup-454885954148'


def _b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b'=').decode()


def _mint(key, **kw):
    now = int(time.time())
    claims = {'identity': DONGLE, 'nbf': now, 'iat': now, 'exp': now + 3600}
    claims.update(kw)
    h = _b64url(json.dumps({'alg': 'RS256', 'typ': 'JWT'}).encode())
    p = _b64url(json.dumps(claims).encode())
    return f'{h}.{p}.{_b64url(key.sign(f"{h}.{p}".encode(), padding.PKCS1v15(), hashes.SHA256()))}'


@pytest.fixture(scope='module')
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(autouse=True)
def cfg(key, monkeypatch):
    pub = key.public_key().public_numbers()
    jwk = {'n': device_pubkey_env._b64u(pub.n), 'e': device_pubkey_env._b64u(pub.e)}
    monkeypatch.setattr(handler, 'DEVICE_PUBKEYS', {DONGLE: jwk, OTHER: jwk})
    monkeypatch.setattr(handler, 'CURVEDB_DONGLES', {DONGLE})
    monkeypatch.setattr(handler, 'CURVEDB_BUCKET', BUCKET)
    monkeypatch.setattr(handler, 'AUTH_SIG_MODE', 'log')   # must NOT weaken the new routes


@pytest.fixture
def s3(monkeypatch):
    m = mock.MagicMock()
    m.generate_presigned_url.side_effect = lambda op, Params, ExpiresIn: f'https://signed/{Params["Key"]}?e={ExpiresIn}'
    monkeypatch.setattr(handler, 's3', lambda: m)
    return m


def _ptr(**kw):
    p = {'sha256': SHA, 'rows_bytes': 723977, 'manifest_sha256': MSHA, 'manifest_bytes': 580,
         'build': '20261016T070820Z', 'approved_utc': '2026-10-19T00:00:00Z'}
    p.update(kw)
    return p


def _serve(s3, ptr):
    s3.get_object.return_value = {'Body': mock.Mock(read=lambda: json.dumps(ptr).encode())}


def _clerr(code):
    return ClientError({'Error': {'Code': code, 'Message': 'x'}}, 'GetObject')


def _get(token, dongle=DONGLE):
    return handler.handler({'rawPath': f'/v1/curvedb/{dongle}/approved',
                            'requestContext': {'http': {'method': 'GET'}},
                            'headers': {'Authorization': f'JWT {token}'}}, None)


def _post(token, body, dongle=DONGLE):
    if not isinstance(body, str):
        body = json.dumps(body)
    return handler.handler({'rawPath': f'/v1/curvedb/{dongle}/report',
                            'requestContext': {'http': {'method': 'POST'}},
                            'headers': {'Authorization': f'JWT {token}'}, 'body': body}, None)


def _auth_cases(key):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return {
        'forged': (_mint(other), DONGLE),
        'expired': (_mint(key, exp=int(time.time()) - 3600), DONGLE),
        'other_dongle_identity': (_mint(key, identity=OTHER), DONGLE),
        'dongle_not_listed': (_mint(key, identity=OTHER), OTHER),
        'garbage': ('x.y.z', DONGLE),
        'empty': ('', DONGLE),
    }


@pytest.mark.parametrize('name', ['forged', 'expired', 'other_dongle_identity', 'dongle_not_listed', 'garbage', 'empty'])
def test_auth_rejects_both_routes(key, s3, name):
    tok, dongle = _auth_cases(key)[name]
    _serve(s3, _ptr())
    for r in (_get(tok, dongle), _post(tok, {'state': 'current'}, dongle)):
        assert r['statusCode'] == 401 and r['body'] == 'unauthorized'
    s3.get_object.assert_not_called()
    s3.put_object.assert_not_called()


def test_get_valid(key, s3):
    _serve(s3, _ptr())
    r = _get(_mint(key))
    assert r['statusCode'] == 200
    b = json.loads(r['body'])
    assert set(b) == {'sha256', 'rows_bytes', 'manifest_sha256', 'manifest_bytes', 'build', 'approved_utc',
                      'rows_url', 'manifest_url'}
    s3.get_object.assert_called_once_with(Bucket=BUCKET, Key='curvedb/approved.json')
    calls = [c.kwargs for c in s3.generate_presigned_url.call_args_list]
    assert {c['Params']['Key'] for c in calls} == {f'curvedb/tables/{SHA}/curvedb_v2_rows.json.zst',
                                                   f'curvedb/tables/{SHA}/manifest.json'}
    assert all(c['ExpiresIn'] == 300 and c['Params']['Bucket'] == BUCKET for c in calls)


def test_pointer_keys_ignored(key, s3):          # a pointer cannot redirect the presign to another key
    _serve(s3, _ptr(rows_key='curvedb/state/secret'))
    _get(_mint(key))
    assert all('secret' not in c.kwargs['Params']['Key'] for c in s3.generate_presigned_url.call_args_list)


def test_missing_pointer_is_404(key, s3):
    s3.get_object.side_effect = _clerr('NoSuchKey')
    r = _get(_mint(key))
    assert r['statusCode'] == 404 and json.loads(r['body']) == {'error': 'none published'}


@pytest.mark.parametrize('code', ['AccessDenied', 'SlowDown', 'InternalError', 'ServiceUnavailable'])
def test_other_s3_errors_are_503(key, s3, code):
    s3.get_object.side_effect = _clerr(code)
    assert _get(_mint(key))['statusCode'] == 503


def test_network_error_is_503(key, s3):
    s3.get_object.side_effect = OSError('boom')
    assert _get(_mint(key))['statusCode'] == 503


@pytest.mark.parametrize('bad', [
    _ptr(sha256='xyz'), _ptr(sha256=SHA.upper()), _ptr(manifest_sha256='1'), _ptr(rows_bytes='5'),
    _ptr(rows_bytes=True), _ptr(manifest_bytes=-1), _ptr(build=None), _ptr(approved_utc=1),
    {k: v for k, v in _ptr().items() if k != 'sha256'}, {k: v for k, v in _ptr().items() if k != 'rows_bytes'},
    ['not', 'a', 'dict']])
def test_malformed_pointer_is_503(key, s3, bad, capsys):
    _serve(s3, bad)
    assert _get(_mint(key))['statusCode'] == 503
    assert 'CURVEDB_POINTER_MALFORMED' in capsys.readouterr().out
    s3.generate_presigned_url.assert_not_called()


def test_unparseable_pointer_is_503(key, s3):
    s3.get_object.return_value = {'Body': mock.Mock(read=lambda: b'{not json')}
    assert _get(_mint(key))['statusCode'] == 503


def test_no_urls_or_token_in_logs(key, s3, capsys):
    _serve(s3, _ptr())
    tok = _mint(key)
    _get(tok)
    out = capsys.readouterr().out
    assert tok not in out and 'signed' not in out


GOOD = {'live_sha': SHA, 'pointer_sha': SHA, 'last_auto_sha': None, 'auto_utc': '2026-10-19T01:02:03Z',
        'loaded': True, 'state': 'installed', 'reason': 'ok', 'free_mb': 1234, 'git_sha': 'deadbeef'}


def test_report_valid(key, s3, capsys):
    r = _post(_mint(key), GOOD)
    assert r['statusCode'] == 200
    kw = s3.put_object.call_args.kwargs
    assert kw['Bucket'] == BUCKET and kw['Key'] == f'curvedb/device/{DONGLE}/report.json'
    stored = json.loads(kw['Body'])
    assert stored['state'] == 'installed' and 'received_utc' in stored
    assert f'CURVEDB_REPORT dongle={DONGLE} state=installed live={SHA[:8]}' in capsys.readouterr().out


def test_report_base64_body(key, s3):
    ev = {'rawPath': f'/v1/curvedb/{DONGLE}/report', 'requestContext': {'http': {'method': 'POST'}},
          'headers': {'Authorization': f'JWT {_mint(key)}'}, 'isBase64Encoded': True,
          'body': base64.b64encode(json.dumps(GOOD).encode()).decode()}
    assert handler.handler(ev, None)['statusCode'] == 200


def test_report_reason_sanitized(key, s3):
    _post(_mint(key), {'state': 'error', 'reason': 'bad\nline\x00'})
    assert json.loads(s3.put_object.call_args.kwargs['Body'])['reason'] == 'bad?line?'


@pytest.mark.parametrize('body', [
    {**GOOD, 'extra': 1}, {**GOOD, 'live_sha': 'nothex'}, {**GOOD, 'pointer_sha': SHA[:-1]},
    {**GOOD, 'state': 'weird'}, {**GOOD, 'reason': 'x' * 201}, {**GOOD, 'loaded': 'yes'},
    {**GOOD, 'free_mb': -5}, {**GOOD, 'free_mb': 1.5}, {**GOOD, 'git_sha': 'a b;c'},
    {'state': 'error', 'reason': 'x' * 3000}, '{bad', '[1]', ''])
def test_report_invalid_400(key, s3, body):
    assert _post(_mint(key), body)['statusCode'] == 400
    s3.put_object.assert_not_called()


def test_report_put_failure_503(key, s3):
    s3.put_object.side_effect = _clerr('AccessDenied')
    assert _post(_mint(key), GOOD)['statusCode'] == 503


def test_wrong_method_not_routed(key, s3):
    r = handler.handler({'rawPath': f'/v1/curvedb/{DONGLE}/approved', 'requestContext': {'http': {'method': 'POST'}},
                         'headers': {'Authorization': f'JWT {_mint(key)}'}}, None)
    assert r['statusCode'] == 404
