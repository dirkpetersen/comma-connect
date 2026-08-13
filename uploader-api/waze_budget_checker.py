"""waze-budget-checker — hourly early-warning on OpenWebNinja Waze spend (pnw-pilot).

Reads the proxy's monthly spend counter (`budget:YYYY-MM` in the comma-waze-cache DynamoDB table,
n = upstream calls) and emails via SNS the first time the month's spend crosses each threshold in
WARN_USD_LEVELS (default "5,20"). A per-level once-per-month flag (`alerted:YYYY-MM:<lvl>`, ~40d TTL)
makes each level fire exactly once per month. The hard cap ($25 / 5000 calls -> HTTP 402) is enforced
separately inside comma-waze-proxy; this is only the early-warnings.

Invoke with {"test": true} to send a TEST email immediately (no thresholds, no flags touched).
"""
import os
import time
from datetime import datetime, timezone

import boto3

TABLE = os.environ.get('WAZE_TABLE', 'comma-waze-cache')
TOPIC = os.environ['SNS_TOPIC_ARN']
LEVELS = sorted(float(x) for x in os.environ.get('WARN_USD_LEVELS', '5,20').split(',') if x.strip())
BUDGET_USD = float(os.environ.get('BUDGET_USD', '25'))
COST = float(os.environ.get('COST_PER_CALL', '0.005'))

_ddb = boto3.resource('dynamodb').Table(TABLE)
_sns = boto3.client('sns')


def _month():
    return f'{datetime.now(timezone.utc):%Y-%m}'


def _lvl_key(x):
    return f'{x:g}'   # 5.0 -> "5", 20.0 -> "20", 7.5 -> "7.5"


def _publish(subject, message):
    _sns.publish(TopicArn=TOPIC, Subject=subject, Message=message)


def handler(event, context):
    event = event or {}
    if event.get('test'):
        _publish(
            '[pnw-pilot] Waze API budget alert — TEST',
            'This is a TEST of the pnw-pilot Waze API (OpenWebNinja police-alert proxy) budget alert.\n\n'
            f'Configured early-warning thresholds: {", ".join("$"+_lvl_key(l) for l in LEVELS)}. '
            f'Hard cap: ${BUDGET_USD:.0f}.\n'
            'If you received this, the alert email path works. No real threshold was crossed.\n\n'
            '-- pnw-pilot / comma-waze-proxy (AWS us-west-2)\n')
        print('TEST email published')
        return {'test': True, 'sent': True}

    month = _month()
    item = _ddb.get_item(Key={'cell': f'budget:{month}'}).get('Item')
    count = int(item.get('n', 0)) if item else 0
    spend = count * COST
    print(f'waze spend {month}: {count} calls = ${spend:.2f}  levels={LEVELS} cap=${BUDGET_USD:.0f}')

    fired = []
    cap_calls = int(BUDGET_USD / COST)
    for lvl in LEVELS:
        if spend < lvl:
            continue
        flag = f'alerted:{month}:{_lvl_key(lvl)}'
        if _ddb.get_item(Key={'cell': flag}).get('Item'):
            continue   # already alerted this level this month
        _publish(
            f'[pnw-pilot] Waze API spend crossed ${_lvl_key(lvl)} (now ${spend:.2f} / cap ${BUDGET_USD:.0f})',
            f'pnw-pilot Waze API (OpenWebNinja police-alert proxy) — spend for {month} has crossed '
            f'${_lvl_key(lvl)} and is now ${spend:.2f} ({count} upstream calls x ${COST}).\n\n'
            f'Hard cap: ${BUDGET_USD:.0f} ({cap_calls} calls) — at the cap the proxy (comma-waze-proxy '
            f'Lambda) returns HTTP 402 and stops calling OpenWebNinja until the UTC month rolls over.\n\n'
            f'To raise the cap: bump the WAZE_BUDGET_USD env var on the comma-waze-proxy Lambda.\n\n'
            f'-- pnw-pilot / comma-waze-proxy (AWS us-west-2)\n')
        _ddb.put_item(Item={'cell': flag, 'n': 1, 'exp': int(time.time()) + 40 * 86400})
        fired.append(_lvl_key(lvl))
        print(f'ALERTED level ${_lvl_key(lvl)} at spend ${spend:.2f}')

    return {'month': month, 'spend': spend, 'fired': fired}
