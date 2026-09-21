from fastapi.testclient import TestClient

from demo.main import app, run_scenario

client = TestClient(app)


def test_health_points_to_real_reconciliation_engine():
    payload = client.get("/health").json()
    assert payload["status"] == "ok"
    assert payload["engine"] == "services/reconciler/app/engine.py"


def test_landing_page_explains_business_problem_before_tech():
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    assert "고객은 주문을 취소했는데" in body
    assert "점주 정산과 포인트가 남았다면?" in body
    assert "장애 감지 및 복구 계획 계산" in body
    assert "실제 프로젝트의" in body


def test_late_cancel_uses_core_engine_and_proposes_compensation():
    result = run_scenario("late-cancel")
    codes = {x["code"] for x in result["repairs"]}
    anomaly_codes = {x["code"] for x in result["anomalies"]}
    assert "REVERSE_SETTLEMENT" in codes
    assert "REVERSE_REWARD" in codes
    assert "REBUILD_PROJECTION" in codes
    assert "settlement_posted_after_prior_cancellation" in anomaly_codes
    assert result["engine"] == "services/reconciler/app/engine.py"
    assert result["before"] == "주문 취소 / 정산 유지 / 90P 유지"
    assert result["after"] == "주문 취소 / 정산 취소 / 90P 회수"


def test_safe_duplicate_is_noop():
    result = run_scenario("duplicate")
    codes = {x["code"] for x in result["repairs"]}
    assert "NO_OP_DUPLICATE" in codes
    assert "MANUAL_REVIEW" not in codes


def test_conflicting_duplicate_requires_manual_review():
    result = run_scenario("conflict")
    codes = {x["code"] for x in result["repairs"]}
    assert "MANUAL_REVIEW" in codes


def test_capacity_drop_marks_promise_at_risk():
    result = run_scenario("capacity-drop")
    codes = {x["code"] for x in result["repairs"]}
    assert "RESLOT_REVIEW" in codes
    assert "AT_RISK" in result["after"]


def test_received_order_and_business_order_diverge_for_late_cancel():
    result = run_scenario("late-cancel")
    received = [x["event_type"] for x in result["received_order"]]
    business = [x["event_type"] for x in result["business_order"]]
    assert received.index("SettlementPosted") < received.index("CommitmentCancelled")
    assert business.index("CommitmentCancelled") < business.index("SettlementPosted")


def test_public_scenario_endpoint():
    response = client.get("/api/scenarios/late-cancel")
    assert response.status_code == 200
    payload = response.json()
    assert payload["order"]["order_id"] == "PP-1208"
    assert payload["engine"] == "services/reconciler/app/engine.py"
