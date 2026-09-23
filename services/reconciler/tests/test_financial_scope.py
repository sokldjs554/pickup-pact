from copy import deepcopy
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.engine import reconcile
from scripts.repair_audit.cases import suite, ev


def request():
    return deepcopy(next(c.request for c in suite() if c.name == 'delayed_cancel'))


def assert_blocked(req, reason):
    result = reconcile(req)
    assert result.decision == 'MANUAL_REVIEW'
    assert reason in result.blocking_reasons
    assert not {'REVERSE_SETTLEMENT','REVERSE_REWARD','REBUILD_PROJECTION'} & set(result.repairs)


def test_partial_cancellation_cannot_authorize_whole_order_reversal():
    req = request()
    req.events[3].payload = {'scope': 'PARTIAL', 'amount': '3000'}
    assert_blocked(req, 'unsupported_partial_cancellation')


def test_distinct_postings_need_amount_attribution_not_one_boolean():
    req = request()
    req.events.append(ev('settle2', 'SettlementPosted', 6, cause='cancel', payload={'amount':'2000'}))
    assert_blocked(req, 'multiple_financial_postings')


@pytest.mark.parametrize('amount', ['NaN','Infinity','0','-1','1.25',True,'not-money'])
def test_invalid_amount_cannot_authorize_reversal(amount):
    req = request()
    req.events[4].payload['amount'] = amount
    assert_blocked(req, 'invalid_financial_amount')


def test_wrong_accounting_unit_cannot_authorize_reversal():
    req = request()
    req.events[4].payload['currency'] = 'PTS'
    assert_blocked(req, 'invalid_financial_unit')


def test_goodwill_points_are_not_automatically_revoked_on_cancellation():
    req = request()
    req.events[5].payload['source'] = 'pickup_pact_breach'
    assert_blocked(req, 'unsupported_reward_policy')


def test_partial_reversal_does_not_mark_full_amount_resolved():
    req = request()
    req.events.append(ev('reverse','SettlementReversed',7,cause='settle',payload={'amount':'1'}))
    assert_blocked(req, 'ambiguous_reversal_attribution')


def test_future_event_schema_is_not_interpreted_as_supported():
    req = request()
    req.events[3].schema_version = 2
    assert_blocked(req, 'unsupported_event_schema')


def test_invalid_capacity_returns_validation_error_not_server_error():
    payload = request().model_dump(mode='json')
    payload['events'][0]['payload']['capacity_units'] = 'not-a-number'
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post('/api/v1/reconcile', json=payload)
    assert response.status_code == 422


def test_naive_timestamp_is_rejected_before_event_ordering():
    payload = request().model_dump(mode='json')
    payload['events'][0]['occurred_at'] = '2026-09-23T00:00:00'
    client = TestClient(app, raise_server_exceptions=False)
    assert client.post('/api/v1/reconcile', json=payload).status_code == 422


def test_missing_amount_cannot_authorize_financial_action():
    req = request()
    del req.events[4].payload['amount']
    assert_blocked(req, 'invalid_financial_amount')
