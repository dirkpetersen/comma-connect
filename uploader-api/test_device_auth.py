"""Offline tests for the device-JWT signature check (check_auth / verify_device_jwt).

Throwaway RSA key generated per run, never committed. Tokens are shaped like openpilot's
common/api.py get_token(): RS256, claims identity / nbf / iat / exp (numeric, as PyJWT emits).
Run: python3 -m pytest uploader-api/test_device_auth.py
"""
import base64
import hashlib
import hmac
import json
import os
import sys
import time

import pytest

os.environ.setdefault('S3_BUCKET', 'test-bucket')
# same values test_handler.py uses (whichever file imports `handler` first wins)
os.environ['AUTH0_DOMAIN'] = 'tenant.us.auth0.com'
os.environ['AUTH0_AUDIENCE'] = 'https://connect-api.test'
sys.path.insert(0, os.path.dirname(__file__))
import handler  # noqa: E402
from tools import device_pubkey_env  # noqa: E402

# no importorskip: a missing `cryptography` must FAIL these tests, not silently skip them (Rule 2)
from cryptography.hazmat.primitives.asymmetric import rsa, padding  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402

DONGLE = '0123456789abcdef'


def _b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b'=').decode()


def _mint(key, claims, alg='RS256', sign=True):
    h = _b64url(json.dumps({'alg': alg, 'typ': 'JWT'}).encode())
    p = _b64url(json.dumps(claims).encode())
    sig = key.sign(f'{h}.{p}'.encode(), padding.PKCS1v15(), hashes.SHA256()) if sign else b''
    return f'{h}.{p}.{_b64url(sig)}'


def _claims(**kw):
    now = int(time.time())
    c = {'identity': DONGLE, 'nbf': now, 'iat': now, 'exp': now + 3600}
    c.update(kw)
    return {k: v for k, v in c.items() if v is not None}


def _event(token, path=f'/v1.4/{DONGLE}/upload_url/'):
    return {'rawPath': path, 'headers': {'Authorization': f'JWT {token}'}}


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(autouse=True)
def configured(key, monkeypatch):
    pub = key.public_key().public_numbers()
    monkeypatch.setattr(handler, 'DEVICE_PUBKEYS',
                        {DONGLE: {'n': device_pubkey_env._b64u(pub.n), 'e': device_pubkey_env._b64u(pub.e)}})
    monkeypatch.setattr(handler, 'ALLOWED_DONGLES', set())
    monkeypatch.setattr(handler, 'AUTH_SIG_MODE', 'log')


def _forgeries(key):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    good = _mint(key, _claims())
    h, p, s = good.split('.')
    tampered_p = _b64url(json.dumps(_claims(exp=int(time.time()) + 10**9)).encode())
    pub_pem = key.public_key().public_bytes(serialization.Encoding.PEM,
                                            serialization.PublicFormat.SubjectPublicKeyInfo)
    hs_h = _b64url(json.dumps({'alg': 'HS256', 'typ': 'JWT'}).encode())
    hs_sig = hmac.new(pub_pem, f'{hs_h}.{p}'.encode(), hashlib.sha256).digest()
    now = int(time.time())
    return {
        'wrong_key': (_mint(other, _claims()), 'bad_sig'),
        'tampered_payload': (f'{h}.{tampered_p}.{s}', 'bad_sig'),
        'alg_none': (_mint(key, _claims(), alg='none', sign=False), 'bad_alg'),
        'alg_hs256_pubkey_secret': (f'{hs_h}.{p}.{_b64url(hs_sig)}', 'bad_alg'),
        'expired': (_mint(key, _claims(nbf=now - 7200, iat=now - 7200, exp=now - 4000)), 'expired'),
        'missing_exp': (_mint(key, _claims(exp=None)), 'no_exp'),
        'nbf_future': (_mint(key, _claims(nbf=now + 3600)), 'not_yet_valid'),
        'malformed_two_parts': ('abc.def', 'malformed'),
        'malformed_garbage_b64': ('!!!.@@@.###', 'malformed:'),
    }


def test_valid_token_passes_both_modes(key, monkeypatch, capsys):
    tok = _mint(key, _claims())
    assert handler.check_auth(_event(tok), DONGLE) is True
    monkeypatch.setattr(handler, 'AUTH_SIG_MODE', 'enforce')
    assert handler.check_auth(_event(tok), DONGLE) is True
    assert 'AUTH_SIG' not in capsys.readouterr().out


def test_skew_within_300s_accepted(key):
    now = int(time.time())
    assert handler.verify_device_jwt(_mint(key, _claims(exp=now - 200)), DONGLE) is None
    assert handler.verify_device_jwt(_mint(key, _claims(nbf=now + 200)), DONGLE) is None


@pytest.mark.parametrize('name', ['wrong_key', 'tampered_payload', 'alg_none', 'alg_hs256_pubkey_secret',
                                  'expired', 'missing_exp', 'nbf_future', 'malformed_two_parts',
                                  'malformed_garbage_b64'])
def test_forgery_log_mode_allows_and_logs(key, capsys, name):
    tok, reason = _forgeries(key)[name]
    assert handler.verify_device_jwt(tok, DONGLE).startswith(reason)
    handler.check_auth(_event(tok), DONGLE)
    out = capsys.readouterr().out
    if name.startswith('malformed'):
        return  # identity pin (unchanged, pre-existing) already rejects undecodable tokens
    assert f'AUTH_SIG_FAIL reason={reason}' in out
    assert f'dongle={DONGLE}' in out and 'action=allow' in out and 'mode=log' in out
    assert tok not in out and tok.split('.')[1] not in out      # token never logged


@pytest.mark.parametrize('name', ['wrong_key', 'tampered_payload', 'alg_none', 'alg_hs256_pubkey_secret',
                                  'expired', 'missing_exp', 'nbf_future'])
def test_forgery_log_mode_proceeds(key, name):
    assert handler.check_auth(_event(_forgeries(key)[name][0]), DONGLE) is True


@pytest.mark.parametrize('name', ['wrong_key', 'tampered_payload', 'alg_none', 'alg_hs256_pubkey_secret',
                                  'expired', 'missing_exp', 'nbf_future', 'malformed_two_parts',
                                  'malformed_garbage_b64'])
def test_forgery_enforce_rejects(key, monkeypatch, name):
    monkeypatch.setattr(handler, 'AUTH_SIG_MODE', 'enforce')
    assert handler.check_auth(_event(_forgeries(key)[name][0]), DONGLE) is False


def test_enforce_end_to_end_401_and_log_route(key, monkeypatch, capsys):
    monkeypatch.setattr(handler, 'AUTH_SIG_MODE', 'enforce')
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ev = _event(_mint(other, _claims()))
    ev['requestContext'] = {'http': {'method': 'GET'}}
    r = handler.handler(ev, None)
    assert r['statusCode'] == 401
    out = capsys.readouterr().out
    assert 'AUTH_SIG_FAIL reason=bad_sig' in out and f'route=/v1.4/{DONGLE}/upload_url/' in out
    assert 'action=reject' in out


def test_devices_route_also_covered(key, monkeypatch):
    monkeypatch.setattr(handler, 'AUTH_SIG_MODE', 'enforce')
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ev = _event(_mint(other, _claims()), path=f'/v1.1/devices/{DONGLE}')
    ev['requestContext'] = {'http': {'method': 'GET'}}
    assert handler.handler(ev, None)['statusCode'] == 401
    ev = _event(_mint(key, _claims()), path=f'/v1.1/devices/{DONGLE}')
    ev['requestContext'] = {'http': {'method': 'GET'}}
    assert handler.handler(ev, None)['statusCode'] == 200


def test_no_key_configured(key, monkeypatch, capsys):
    monkeypatch.setattr(handler, 'DEVICE_PUBKEYS', {})
    tok = _mint(key, _claims())
    assert handler.check_auth(_event(tok), DONGLE) is True
    assert f'AUTH_SIG_NOKEY reason=nokey dongle={DONGLE}' in capsys.readouterr().out
    monkeypatch.setattr(handler, 'AUTH_SIG_MODE', 'enforce')
    assert handler.check_auth(_event(tok), DONGLE) is False


def test_unknown_mode_fails_closed(key, monkeypatch):
    monkeypatch.setattr(handler, 'AUTH_SIG_MODE', 'enforced')   # typo must not weaken
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert handler.check_auth(_event(_mint(other, _claims())), DONGLE) is False


def test_identity_pin_and_allowlist_unchanged(key, monkeypatch):
    tok = _mint(key, _claims(identity='ffffffffffffffff'))
    assert handler.check_auth(_event(tok), DONGLE) is False          # even in log mode
    monkeypatch.setattr(handler, 'ALLOWED_DONGLES', {'aaaaaaaaaaaaaaaa'})
    assert handler.check_auth(_event(_mint(key, _claims())), DONGLE) is False


def test_missing_header_rejected_log_mode():
    assert handler.check_auth({'headers': {}}, DONGLE) is False


def _env_key(bits=2048, e=65537):
    k = rsa.generate_private_key(public_exponent=e, key_size=bits)
    p = k.public_key().public_numbers()
    return {'n': device_pubkey_env._b64u(p.n), 'e': device_pubkey_env._b64u(p.e)}


def test_pubkey_validation(monkeypatch, capsys):
    good, weak, odd_e = _env_key(), _env_key(1024), _env_key(2048, 3)
    monkeypatch.setenv('DEVICE_PUBKEYS', json.dumps({
        DONGLE: good, 'aaaaaaaaaaaaaaaa': weak, 'bbbbbbbbbbbbbbbb': odd_e,
        'cccccccccccccccc': {'n': 'x'}, 'dddddddddddddddd': 'junk'}))
    keys = handler._load_device_pubkeys()
    assert set(keys) == {DONGLE}
    out = capsys.readouterr().out
    assert out.count('AUTH_SIG_CONFIG_ERROR') == 4
    for d in ('aaaaaaaaaaaaaaaa', 'bbbbbbbbbbbbbbbb', 'cccccccccccccccc', 'dddddddddddddddd'):
        assert f'dongle={d}' in out


def test_weak_key_dongle_treated_as_nokey(key, monkeypatch, capsys):
    monkeypatch.setenv('DEVICE_PUBKEYS', json.dumps({DONGLE: _env_key(1024)}))
    monkeypatch.setattr(handler, 'DEVICE_PUBKEYS', handler._load_device_pubkeys())
    monkeypatch.setattr(handler, 'AUTH_SIG_MODE', 'enforce')
    capsys.readouterr()
    assert handler.check_auth(_event(_mint(key, _claims())), DONGLE) is False
    assert 'AUTH_SIG_NOKEY' in capsys.readouterr().out


def test_bad_env_json_logged(monkeypatch, capsys):
    monkeypatch.setenv('DEVICE_PUBKEYS', '{not json')
    assert handler._load_device_pubkeys() == {}
    assert 'AUTH_SIG_CONFIG_ERROR' in capsys.readouterr().out


class TestPubkeyTool:
    def _roundtrip(self, key, text):
        env = json.loads(device_pubkey_env.key_to_env(text, DONGLE))
        pub = key.public_key().public_numbers()
        assert int.from_bytes(handler._b64url_decode(env[DONGLE]['n']), 'big') == pub.n
        assert int.from_bytes(handler._b64url_decode(env[DONGLE]['e']), 'big') == pub.e
        tok = _mint(key, _claims())
        h, p, s = tok.split('.')
        assert handler._rs256_verify(f'{h}.{p}'.encode(), handler._b64url_decode(s), env[DONGLE])

    def test_spki_pem(self, key):
        pem = key.public_key().public_bytes(serialization.Encoding.PEM,
                                            serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        self._roundtrip(key, pem)

    def test_pkcs1_pem(self, key):
        pem = key.public_key().public_bytes(serialization.Encoding.PEM,
                                            serialization.PublicFormat.PKCS1).decode()
        self._roundtrip(key, pem)

    def test_openssh(self, key):
        line = key.public_key().public_bytes(serialization.Encoding.OpenSSH,
                                             serialization.PublicFormat.OpenSSH).decode()
        self._roundtrip(key, line)

    def test_private_key_refused(self, key):
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
        with pytest.raises(ValueError):
            device_pubkey_env.key_to_env(pem, DONGLE)
