"""Offline tests for waze_handler's timestamp parsing and budget-cap math — no AWS, no network.

Run: python3 -m pytest uploader-api/test_waze_handler.py
"""
import json
import os

os.environ.setdefault('WAZE_TABLE', 'test-waze-cache')

import waze_handler as w  # noqa: E402


def test_iso_to_epoch_ms_with_fractional_and_z():
    # Value independently verified via python (fromisoformat, calendar.timegm) and GNU `date -u`.
    assert w._iso_to_epoch_ms('2026-08-13T14:49:09.000Z') == 1786632549000


def test_iso_to_epoch_ms_without_fractional():
    assert w._iso_to_epoch_ms('2026-08-13T14:49:09Z') == 1786632549000


def test_iso_to_epoch_ms_without_z():
    assert w._iso_to_epoch_ms('2026-08-13T14:49:09') == 1786632549000


def test_iso_to_epoch_ms_bad_input_returns_none():
    assert w._iso_to_epoch_ms(None) is None
    assert w._iso_to_epoch_ms('') is None
    assert w._iso_to_epoch_ms('not-a-date') is None


def test_budget_calls_default_is_5000():
    assert w.BUDGET_USD == 25.0
    assert w.COST_PER_CALL == 0.005
    assert w.BUDGET_CALLS == 5000


def test_budget_month_key_format():
    key = w._budget_month_key()
    assert key.startswith('budget:')
    assert len(key) == len('budget:YYYY-MM')


# --- upstream timeout ceiling + failure-path logging (2026-08-18) -------------------------------

def test_upstream_timeout_leaves_headroom_under_function_timeout():
    # The deployed comma-waze-proxy function timeout is 8 s. The urllib timeout must stay strictly
    # below it, or a hung upstream kills the invocation instead of returning the documented
    # 200 + error-tag contract the device relies on (PoliceUpdater._parse_proxy_body).
    assert w.UPSTREAM_TIMEOUT_S == 6
    assert w.UPSTREAM_TIMEOUT_S < 8


class _FakeTable:
    def get_item(self, **kwargs):
        return {}                      # always a cache MISS

    def put_item(self, **kwargs):
        pass


def _alerts_event():
    return {'rawPath': '/alerts', 'headers': {'x-device-id': 'testdev'},
            'queryStringParameters': {'lat': '47.6', 'lon': '-122.3'}}


def _stub_caps(monkeypatch):
    w._l1.clear()                      # L1 is module state -- a warm cell would skip the fetch
    monkeypatch.setattr(w, 'table', lambda: _FakeTable())
    monkeypatch.setattr(w, '_budget_count', lambda: 0)
    monkeypatch.setattr(w, '_device_count', lambda dev_id: 0)
    monkeypatch.setattr(w, '_budget_inc', lambda: None)
    monkeypatch.setattr(w, '_device_inc', lambda dev_id: None)


def test_upstream_failure_tags_response_and_logs(monkeypatch, capsys):
    _stub_caps(monkeypatch)

    def boom(clat, clon):
        raise TimeoutError('timed out')
    monkeypatch.setattr(w, '_fetch_waze', boom)

    resp = w.handler(_alerts_event(), None)
    body = json.loads(resp['body'])

    # contract the device depends on: 200 + empty alerts + error tag (never a false 'clear')
    assert resp['statusCode'] == 200
    assert body['alerts'] == []
    assert body['error'] == 'upstream TimeoutError'

    # the regression this guards: this branch used to be silent in CloudWatch
    out = capsys.readouterr().out
    assert 'upstream_error TimeoutError' in out
    assert 'cell=' in out
    assert 'elapsed=' in out
    assert f'timeout={w.UPSTREAM_TIMEOUT_S}s' in out


def test_upstream_failure_is_not_cached(monkeypatch):
    _stub_caps(monkeypatch)
    monkeypatch.setattr(w, '_fetch_waze', lambda clat, clon: (_ for _ in ()).throw(TimeoutError()))
    w.handler(_alerts_event(), None)
    assert w._l1 == {}                 # errors must never be cached -> next poll retries


def test_upstream_success_does_not_log_an_error(monkeypatch, capsys):
    _stub_caps(monkeypatch)
    monkeypatch.setattr(w, '_fetch_waze', lambda clat, clon: [{'type': 'POLICE'}])

    resp = w.handler(_alerts_event(), None)
    body = json.loads(resp['body'])

    assert resp['statusCode'] == 200
    assert 'error' not in body
    assert 'upstream_error' not in capsys.readouterr().out


# --- policetier2pnw: carry num_thumbs_up so the device can grade confirmed/unconfirmed -----------

class _FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _upstream_alert(**over):
    a = {'type': 'POLICE', 'latitude': 47.06, 'longitude': -122.78, 'alert_id': 'alert-1',
         'publish_datetime_utc': '2026-08-18T03:05:27.000Z', 'street': 'I-5', 'city': 'Lacey',
         'num_thumbs_up': 4, 'alert_confidence': 0, 'alert_reliability': 0}
    a.update(over)
    return a


def _fetch_with(monkeypatch, alerts):
    monkeypatch.setattr(w.urllib.request, 'urlopen',
                        lambda req, timeout=None: _FakeResp({'data': {'alerts': alerts}}))
    return w._fetch_waze(47.0, -122.0)


def test_thumbs_up_is_carried_through(monkeypatch):
    # Waze staff: report lifetime "depends on number of upvotes" -- this is the field the device
    # grades confirmed/unconfirmed on, so dropping it here silently disables the whole tier model.
    out = _fetch_with(monkeypatch, [_upstream_alert()])
    assert len(out) == 1
    assert out[0]['thumbs'] == 4


def test_missing_thumbs_up_becomes_none_not_an_error(monkeypatch):
    a = _upstream_alert()
    del a['num_thumbs_up']
    out = _fetch_with(monkeypatch, [a])
    assert out[0]['thumbs'] is None


def test_non_police_alerts_are_still_filtered_out(monkeypatch):
    out = _fetch_with(monkeypatch, [_upstream_alert(type='JAM'), _upstream_alert()])
    assert len(out) == 1 and out[0]['thumbs'] == 4
