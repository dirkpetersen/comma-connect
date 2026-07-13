"""Offline tests for the RS256 JWT verification — no AWS, no network.

Generates a throwaway RSA key, publishes it as a JWKS via monkeypatch, mints tokens, and
asserts the pure-stdlib verifier accepts valid ones and rejects every forgery class.
Run: python3 -m pytest uploader-api/test_handler.py  (needs `cryptography` for key-gen only).
"""
import base64
import json
import os
import time

import pytest

os.environ.setdefault('S3_BUCKET', 'test-bucket')
os.environ['AUTH0_DOMAIN'] = 'tenant.us.auth0.com'
os.environ['AUTH0_AUDIENCE'] = 'https://connect-api.test'

import handler  # noqa: E402

crypto = pytest.importorskip("cryptography")
from cryptography.hazmat.primitives.asymmetric import rsa, padding  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402


def _b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b'=').decode()


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(autouse=True)
def _jwks(key, monkeypatch):
    pub = key.public_key().public_numbers()
    n = pub.n.to_bytes((pub.n.bit_length() + 7) // 8, 'big')
    e = pub.e.to_bytes((pub.e.bit_length() + 7) // 8, 'big')
    jwk = {'kid': 'test-kid', 'kty': 'RSA', 'alg': 'RS256', 'use': 'sig',
           'n': _b64url(n), 'e': _b64url(e)}
    monkeypatch.setattr(handler, '_jwks', lambda force=False: {'test-kid': jwk})


def _make_token(key, claims, kid='test-kid', alg='RS256'):
    header = {'alg': alg, 'typ': 'JWT', 'kid': kid}
    h = _b64url(json.dumps(header).encode())
    p = _b64url(json.dumps(claims).encode())
    signing_input = f'{h}.{p}'.encode()
    sig = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f'{h}.{p}.{_b64url(sig)}'


def _event(token):
    return {'headers': {'Authorization': f'Bearer {token}'}}


def _good_claims():
    now = int(time.time())
    return {'sub': 'google-oauth2|123', 'email': 'a@b.co',
            'iss': 'https://tenant.us.auth0.com/', 'aud': 'https://connect-api.test',
            'iat': now, 'exp': now + 3600}


class TestVerifyAuth0:
    def test_valid_token_accepted(self, key):
        claims = handler.verify_auth0(_event(_make_token(key, _good_claims())))
        assert claims and claims['sub'] == 'google-oauth2|123'

    def test_aud_as_list_accepted(self, key):
        c = _good_claims()
        c['aud'] = ['https://connect-api.test', 'https://other']
        assert handler.verify_auth0(_event(_make_token(key, c)))

    def test_expired_rejected(self, key):
        c = _good_claims()
        c['exp'] = int(time.time()) - 10
        assert handler.verify_auth0(_event(_make_token(key, c))) is None

    def test_wrong_issuer_rejected(self, key):
        c = _good_claims()
        c['iss'] = 'https://evil.us.auth0.com/'
        assert handler.verify_auth0(_event(_make_token(key, c))) is None

    def test_wrong_audience_rejected(self, key):
        c = _good_claims()
        c['aud'] = 'https://some-other-api'
        assert handler.verify_auth0(_event(_make_token(key, c))) is None

    def test_alg_none_rejected(self, key):
        # alg confusion: attacker sets alg=none with an empty signature
        c = _good_claims()
        h = _b64url(json.dumps({'alg': 'none', 'typ': 'JWT', 'kid': 'test-kid'}).encode())
        p = _b64url(json.dumps(c).encode())
        assert handler.verify_auth0(_event(f'{h}.{p}.')) is None

    def test_hs256_forgery_rejected(self, key):
        # alg confusion: sign with HS256 using the public key as the HMAC secret
        import hmac as _hmac
        import hashlib
        pub_pem = key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        h = _b64url(json.dumps({'alg': 'HS256', 'typ': 'JWT', 'kid': 'test-kid'}).encode())
        p = _b64url(json.dumps(_good_claims()).encode())
        sig = _hmac.new(pub_pem, f'{h}.{p}'.encode(), hashlib.sha256).digest()
        assert handler.verify_auth0(_event(f'{h}.{p}.{_b64url(sig)}')) is None

    def test_tampered_payload_rejected(self, key):
        tok = _make_token(key, _good_claims())
        h, _, s = tok.split('.')
        evil = _b64url(json.dumps({**_good_claims(), 'sub': 'attacker'}).encode())
        assert handler.verify_auth0(_event(f'{h}.{evil}.{s}')) is None

    def test_unknown_kid_rejected(self, key):
        assert handler.verify_auth0(_event(_make_token(key, _good_claims(), kid='nope'))) is None

    def test_malformed_rejected(self, key):
        assert handler.verify_auth0({'headers': {'Authorization': 'Bearer not.a.jwt'}}) is None
        assert handler.verify_auth0({'headers': {}}) is None

    def test_device_jwt_prefix_not_accepted_as_bearer(self, key):
        # a device "JWT <token>" must NOT satisfy the Bearer verifier
        tok = _make_token(key, _good_claims())
        assert handler.verify_auth0({'headers': {'Authorization': f'JWT {tok}'}}) is None
