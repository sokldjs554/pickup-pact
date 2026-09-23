from fastapi.testclient import TestClient
import pytest

from app.main import app
from app.engine import reconcile
from app.models import ReconcileRequest
from app import persistence
from test_repair_safety import cancelled, event, prefix

client = TestClient(app)


def test_http_projection_rebuild_rejects_conflicting_evidence_before_writing(monkeypatch):
    written = []
    monkeypatch.setattr(persistence, "rebuild_projection", lambda result: written.append(result) or {})
    request = ReconcileRequest(events=cancelled() + [
        event("x", "SettlementPosted", 4, payload={"amount": "9000"}),
        event("x", "SettlementPosted", 4, payload={"amount": "1"}),
    ])
    response = client.post("/api/v1/projections/rebuild", json=request.model_dump(mode="json"))
    assert response.status_code == 409, response.text
    assert written == []
    assert response.json()["detail"]["decision"] == "MANUAL_REVIEW"


def test_projection_persistence_rejects_waiting_decision_before_opening_db(monkeypatch):
    import sys
    from types import SimpleNamespace
    def forbidden_connection(*args, **kwargs):
        pytest.fail("blocked evidence must not open a database connection")
    monkeypatch.setenv("POSTGRES_DSN", "unreachable-test-dsn")
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=forbidden_connection))
    request = ReconcileRequest(events=cancelled() + [event("x", "SettlementPosted", 4, cause="missing")])
    with pytest.raises(ValueError, match="not executable"):
        persistence.rebuild_projection(reconcile(request))


def test_http_valid_projection_still_reaches_persistence(monkeypatch):
    written = []
    def write(result):
        written.append(result)
        return result.canonical_state.model_dump()
    monkeypatch.setattr(persistence, "rebuild_projection", write)
    request = ReconcileRequest(events=prefix())
    response = client.post("/api/v1/projections/rebuild", json=request.model_dump(mode="json"))
    assert response.status_code == 200, response.text
    assert len(written) == 1
    assert response.json()["projection"]["status"] == "CONFIRMED"


def test_http_wait_returns_missing_evidence_without_financial_action():
    request = ReconcileRequest(events=cancelled() + [event("x", "SettlementPosted", 4, cause="not-here")])
    response = client.post("/api/v1/reconcile", json=request.model_dump(mode="json"))
    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "WAIT_FOR_EVIDENCE"
    assert body["missing_event_ids"] == ["not-here"]
    assert body["repairs"] == ["MANUAL_REVIEW"]


def test_cancellation_request_is_not_accepted_as_a_committed_cancellation():
    request = ReconcileRequest(events=prefix()).model_dump(mode="json")
    pending = event("request", "CommitmentCancelled", 3).model_dump(mode="json")
    pending["event_type"] = "CancellationRequested"
    request["events"].append(pending)
    response = client.post("/api/v1/reconcile", json=request)
    assert response.status_code == 422
