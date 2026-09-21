from fastapi.testclient import TestClient
from demo.main import app, reconcile

client = TestClient(app)

def test_health():
    assert client.get("/health").json()["status"] == "ok"

def test_late_cancel_compensates_money_and_reward():
    result = reconcile("late-cancel")
    commands = {x["command"] for x in result["repairs"]}
    assert "REVERSE_SETTLEMENT" in commands
    assert "REVERSE_REWARD" in commands
    assert result["state_after"] == "CANCELLED + COMPENSATED"

def test_safe_duplicate_is_noop():
    result = reconcile("duplicate")
    assert any(x["command"] == "NO_OP_DUPLICATE" for x in result["repairs"])

def test_conflicting_duplicate_is_quarantined():
    result = reconcile("conflict")
    commands = {x["command"] for x in result["repairs"]}
    assert {"QUARANTINE_EVENT", "MANUAL_REVIEW"} <= commands

def test_capacity_drop_marks_promise_at_risk():
    result = reconcile("capacity-drop")
    assert result["state_after"] == "AT_RISK"

def test_public_scenario_endpoint():
    response = client.get("/api/scenarios/late-cancel")
    assert response.status_code == 200
    assert response.json()["engine"] == "deterministic-temporal-reconciler/v1"
