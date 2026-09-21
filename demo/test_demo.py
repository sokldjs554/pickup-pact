from fastapi.testclient import TestClient
from demo.main import app, reconcile

client = TestClient(app)


def test_health():
    assert client.get("/health").json()["status"] == "ok"


def test_landing_page_explains_business_problem_in_plain_korean():
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    assert "고객은 주문을 취소했는데" in body
    assert "점주 정산과 포인트가 남았을까?" in body
    assert "카페 스마트오더 · 예약 픽업 장애 복구 데모" in body
    assert "문제 재현 → 백엔드 복구 실행" in body


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
