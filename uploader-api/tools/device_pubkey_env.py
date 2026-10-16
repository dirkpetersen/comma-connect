#!/usr/bin/env python3
"""Turn a device RSA PUBLIC key into the DEVICE_PUBKEYS env JSON for the uploader Lambda.

No network, stdlib only. Accepts a PEM ("BEGIN PUBLIC KEY" or "BEGIN RSA PUBLIC KEY") or an
OpenSSH "ssh-rsa AAAA..." line. Prints {"<dongle>": {"n": ..., "e": ...}}.

  ssh comma@comma-remote cat /persist/comma/id_rsa.pub | tools/device_pubkey_env.py <dongle_id>
"""
import base64
import json
import re
import sys


def _der(buf, i):
    """Parse one DER TLV at buf[i]; returns (tag, value_bytes, next_index)."""
    tag = buf[i]
    ln = buf[i + 1]
    i += 2
    if ln & 0x80:
        nb = ln & 0x7f
        ln = int.from_bytes(buf[i:i + nb], 'big')
        i += nb
    return tag, buf[i:i + ln], i + ln


def _rsa_from_der(der):
    tag, body, _ = _der(der, 0)
    assert tag == 0x30, 'not a DER SEQUENCE'
    t1, v1, nxt = _der(body, 0)
    if t1 == 0x30:                               # SubjectPublicKeyInfo: algId SEQ, BIT STRING
        t2, bits, _ = _der(body, nxt)
        assert t2 == 0x03, 'SPKI without BIT STRING'
        return _rsa_from_der(bits[1:])           # skip unused-bits byte
    assert t1 == 0x02, 'unexpected DER layout'   # PKCS#1: INTEGER n, INTEGER e
    t2, v2, _ = _der(body, nxt)
    return int.from_bytes(v1, 'big'), int.from_bytes(v2, 'big')


def _rsa_from_ssh(line):
    blob = base64.b64decode(line.split()[1])
    parts, i = [], 0
    while i < len(blob):
        ln = int.from_bytes(blob[i:i + 4], 'big')
        parts.append(blob[i + 4:i + 4 + ln])
        i += 4 + ln
    assert parts[0] == b'ssh-rsa', 'not an ssh-rsa key'
    return int.from_bytes(parts[2], 'big'), int.from_bytes(parts[1], 'big')


def _b64u(n):
    return base64.urlsafe_b64encode(n.to_bytes((n.bit_length() + 7) // 8, 'big')).rstrip(b'=').decode()


def key_to_env(text, dongle_id):
    if not re.fullmatch(r'[0-9a-f]{16}', dongle_id):
        raise ValueError(f'bad dongle id {dongle_id!r}')
    text = text.strip()
    if text.startswith('ssh-rsa '):
        n, e = _rsa_from_ssh(text)
    else:
        m = re.search(r'-----BEGIN (?:RSA )?PUBLIC KEY-----(.+?)-----END', text, re.S)
        if not m:
            raise ValueError('no PEM public key found (a PRIVATE key is never accepted here)')
        n, e = _rsa_from_der(base64.b64decode(''.join(m.group(1).split())))
    return json.dumps({dongle_id: {'n': _b64u(n), 'e': _b64u(e)}}, separators=(',', ':'))


if __name__ == '__main__':
    if len(sys.argv) not in (2, 3):
        sys.exit('usage: device_pubkey_env.py <dongle_id> [pubkey_file]  (stdin if no file)')
    src = open(sys.argv[2]).read() if len(sys.argv) == 3 else sys.stdin.read()
    print(key_to_env(src, sys.argv[1]))
