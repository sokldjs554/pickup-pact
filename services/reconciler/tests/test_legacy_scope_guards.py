"""Retained evidence must not regain automatic authority after scope expansion."""
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from app import persistence
from app.engine import reconcile
from app.main import app
from app.models import ReconcileRequest
from scripts.repair_audit.cases import suite
from test_repair_safety import prefix


@pytest.mark.parametrize('marker', [
    {'partial': True}, {'partial_refund': True},
    {'items': [{'sku': 'americano', 'quantity': 1}]},
    {'line_items': [{'sku': 'americano', 'quantity': 1}]},
    {'scope': 'PARTIAL', 'amount': 3000},
])
def test_legacy_partial_markers_never_default_to_full_reversal(marker):
    request = deepcopy(next(case.request for case in suite() if case.name == 'delayed_cancel'))
    cancellation = next(event for event in request.events if event.event_type == 'CommitmentCancelled')
    cancellation.payload.update(marker)
    result = reconcile(request)
    assert result.decision == 'MANUAL_REVIEW'
    assert result.financial_actions == []
    assert not {'REVERSE_SETTLEMENT', 'REVERSE_REWARD', 'REBUILD_PROJECTION'} & set(result.repairs)


@pytest.mark.parametrize('index', [0, 1, 2])
def test_future_schema_without_cancellation_cannot_rebuild_projection(monkeypatch, index):
    request = ReconcileRequest(events=prefix())
    request.events[index].schema_version = 2
    result = reconcile(request)
    assert result.decision == 'MANUAL_REVIEW'
    assert 'unsupported_event_schema' in result.blocking_reasons
    writes = []
    monkeypatch.setattr(persistence, 'rebuild_projection', lambda result: writes.append(result) or {})
    response = TestClient(app).post('/api/v1/projections/rebuild', json=request.model_dump(mode='json'))
    assert response.status_code == 409
    assert writes == []
