from copy import deepcopy
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.engine import reconcile
from app.models import ReconcileRequest
from scripts.repair_audit.cases import suite, ev


def request():
    return deepcopy(next(c.request for c in suite() if c.name == 'delayed_cancel'))


def assert_blocked(req, reason):
    result = reconcile(req)
    assert result.decision == 'MANUAL_REVIEW'
    assert reason in result.blocking_reasons
    assert not {'REVERSE_SETTLEMENT','REVERSE_REWARD','REBUILD_PROJECTION'} & set(result.repairs)
    assert result.financial_actions == []


def test_partial_cancellation_without_explicit_allocation_is_blocked():
    req = request()
    req.events[3] = req.events[3].model_copy(update={
        'event_id': 'partial-cancel',
        'event_type': 'PartialCancellationApplied',
        'payload': {'scope': 'PARTIAL', 'amount': '3000'},
    })
    assert_blocked(req, 'partial_cancellation_allocation_required')


def test_partial_cancellation_executes_only_explicit_source_allocation():
    req = request()
    req.events[3] = req.events[3].model_copy(update={
        'event_id': 'partial-cancel',
        'event_type': 'PartialCancellationApplied',
        'payload': {
            'scope': 'PARTIAL',
            'amount': '3000',
            'allocations': [
                {'target_event_id': 'settle', 'amount': '3000', 'unit': 'KRW'},
            ],
        },
    })
    req.events[4] = req.events[4].model_copy(update={'causation_id': 'partial-cancel'})
    result = reconcile(req)
    assert result.decision == 'AUTO'
    assert [a.model_dump() for a in result.financial_actions] == [{
        'cancellation_event_id': 'partial-cancel',
        'target_event_id': 'settle',
        'repair': 'REVERSE_SETTLEMENT',
        'amount': 3000,
        'unit': 'KRW',
    }]
    assert 'REVERSE_REWARD' not in result.repairs
    assert result.canonical_state.status == 'CONFIRMED'


def test_full_cancellation_with_multiple_postings_returns_one_action_per_open_posting():
    req = request()
    req.events = [event for event in req.events if event.event_type != 'RewardGranted']
    req.events.append(ev(
        'settle2',
        'SettlementPosted',
        6,
        cause='cancel',
        payload={'amount':'2000','currency':'KRW'},
    ))
    result = reconcile(req)
    assert result.decision == 'AUTO'
    assert [(a.target_event_id, a.amount, a.unit) for a in result.financial_actions] == [
        ('settle', 9000, 'KRW'),
        ('settle2', 2000, 'KRW'),
    ]


def test_partial_allocation_cannot_exceed_open_balance():
    req = request()
    req.events[3] = req.events[3].model_copy(update={
        'event_id': 'partial-cancel',
        'event_type': 'PartialCancellationApplied',
        'payload': {
            'scope': 'PARTIAL',
            'amount': '10000',
            'allocations': [
                {'target_event_id': 'settle', 'amount': '10000', 'unit': 'KRW'},
            ],
        },
    })
    req.events[4] = req.events[4].model_copy(update={'causation_id': 'partial-cancel'})
    assert_blocked(req, 'partial_allocation_exceeds_open_balance')


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
    result = reconcile(req)
    assert result.decision == 'AUTO'
    assert [(a.target_event_id, a.repair) for a in result.financial_actions] == [
        ('settle', 'REVERSE_SETTLEMENT')
    ]


def test_partial_reversal_only_repairs_the_remaining_amount():
    req = request()
    req.events.append(ev(
        'reverse','SettlementReversed',7,cause='settle',
        payload={'amount':'1000','currency':'KRW'}
    ))
    result = reconcile(req)
    settlement = next(a for a in result.financial_actions if a.repair == 'REVERSE_SETTLEMENT')
    assert settlement.amount == 8000
    assert result.canonical_state.settled is True


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


@pytest.mark.parametrize('unit_field',['currency','unit'])
def test_reversal_cannot_change_accounting_unit(unit_field):
    req=request()
    req.events.append(ev(
        'reverse','SettlementReversed',7,cause='settle',
        payload={'amount':'9000',unit_field:'PTS'}
    ))
    assert_blocked(req,'invalid_reversal_unit')


def test_reversal_cannot_reference_a_different_source_posting():
    req=request()
    req.events.append(ev(
        'reverse','SettlementReversed',7,
        payload={'amount':'9000','source_event_id':'not-this-settlement'}
    ))
    assert_blocked(req,'missing_financial_posting')


def test_reversal_without_any_original_posting_is_not_resolved():
    req=request()
    req.events=[e for e in req.events if e.event_type!='SettlementPosted']
    req.events.append(ev('reverse','SettlementReversed',7,payload={'amount':'9000'}))
    assert_blocked(req,'ambiguous_reversal_attribution')
