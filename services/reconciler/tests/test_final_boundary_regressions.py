"""Regression cases for safety guards lost during financial-allocation expansion."""
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from app.engine import reconcile
from app.main import app
from app.models import ReconcileRequest
from scripts.repair_audit.cases import suite
from test_repair_safety import prefix

MUTATING = {"REVERSE_SETTLEMENT", "REVERSE_REWARD", "REBUILD_PROJECTION", "RESLOT_REVIEW"}


@pytest.mark.parametrize("marker", [
    {"partial": True},
    {"partial_refund": True},
    {"items": [{"sku": "americano", "quantity": 1}]},
    {"line_items": [{"sku": "americano", "quantity": 1}]},
])
@pytest.mark.parametrize("explicit_full", [False, True])
@pytest.mark.parametrize("reversed_input", [False, True])
def test_legacy_partial_hint_never_authorizes_whole_posting(marker, explicit_full, reversed_input):
    # Removing the partial-hint guard must turn this into an unsafe 9,000 KRW action.
    request = deepcopy(next(case.request for case in suite() if case.name == "delayed_cancel"))
    cancel = next(event for event in request.events if event.event_type == "CommitmentCancelled")
    cancel.payload = {**marker, "amount": "3000"}
    if explicit_full:
        cancel.payload["scope"] = "FULL"
    if reversed_input:
        request.events.reverse()

    result = reconcile(request)

    assert result.decision == "MANUAL_REVIEW", result.model_dump()
    assert "unsupported_partial_cancellation" in result.blocking_reasons
    assert result.financial_actions == []
    assert not MUTATING.intersection(result.repairs)
    assert cancel.event_id in result.evidence_event_ids


@pytest.mark.parametrize("future_index", [0, 1, 2])
@pytest.mark.parametrize("reversed_input", [False, True])
def test_future_schema_without_cancellation_is_not_executable(future_index, reversed_input):
    # A financial-only schema check used to skip these ordinary active orders.
    events = prefix()
    events[future_index].schema_version = 2
    if reversed_input:
        events.reverse()

    result = reconcile(ReconcileRequest(events=events))

    assert result.decision == "MANUAL_REVIEW", result.model_dump()
    assert "unsupported_event_schema" in result.blocking_reasons
    assert result.canonical_state.status == "UNRESOLVED"
    assert not MUTATING.intersection(result.repairs)
    assert result.financial_actions == []
    assert set(result.source_event_ids) == {"hold", "pay", "confirmed"}


def test_future_schema_projection_is_rejected_before_storage(monkeypatch):
    from app import persistence

    calls = []

    def storage(result):
        calls.append(result)
        return {}

    monkeypatch.setattr(persistence, "rebuild_projection", storage)
    events = prefix()
    events[2].schema_version = 2
    request = ReconcileRequest(events=events)
    with TestClient(app) as client:
        response = client.post("/api/v1/projections/rebuild", json=request.model_dump(mode="json"))

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"] == "unresolved_reconciliation_evidence"
    assert calls == []
