"""Offline tests for waze_handler's timestamp parsing and budget-cap math — no AWS, no network.

Run: python3 -m pytest uploader-api/test_waze_handler.py
"""
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
