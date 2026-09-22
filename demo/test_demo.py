from __future__ import annotations

from fastapi.testclient import TestClient

from demo.main import app, run_scenario

client = TestClient(app)


def create_session() -> str:
    response = client.post("/api/demo/sessions")
    assert response.status_code == 200
    return response.json()["session_id"]


def get_state(session_id: str) -> dict:
    response = client.get(f"/api/demo/sessions/{session_id}")
    assert response.status_code == 200
    return response.json()


def create_order(session_id: str, *, units: int = 2, total: int = 9000) -> dict:
    response = client.post(
        f"/api/demo/sessions/{session_id}/orders",
        json={
            "store": "패스카페 테스트점",
            "items": "아메리카노 2잔",
            "total": total,
            "pickup_at": "12:30",
            "units": units,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["state"]


def test_health_points_to_real_reconciliation_engine():
    payload = client.get("/health").json()
    assert payload["status"] == "ok"
    assert payload["engine"] == "services/reconciler/app/engine.py"
    assert payload["release_commit"]

def test_customer_catalog_is_real_demo_api_data():
    customer = client.get("/api/demo/customer")
    assert customer.status_code == 200
    assert customer.json()["label"] == "체험 손님"

    response = client.get("/api/demo/catalog")
    assert response.status_code == 200
    stores = response.json()
    assert len(stores) >= 3
    assert stores[0]["name"] == "패스카페 강남역점"
    assert stores[0]["pickup_minutes"] > 0
    assert any(item["name"] == "아메리카노" for item in stores[0]["menu"])
    assert all(item["price"] > 0 for store in stores for item in store["menu"])



def test_landing_page_exposes_guided_and_expert_layers():
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    for label in [
        "커피, 미리 주문해요.",
        "근처 매장",
        "장바구니 보기",
        "내 주문",
        "체험 손님",
        "메뉴 보기",
        "주문 취소",
        "주문 흐름",
        "매장 처리량",
        "장애 주입",
        "정합성 복구",
        "정산 · 감사",
        "정합성 다시 계산",
        "정상 정산 반영",
        "포인트 적립",
    ]:
        assert label in body


def test_fixed_late_cancel_scenario_still_uses_core_engine():
    result = run_scenario("late-cancel")
    codes = {x["code"] for x in result["repairs"]}
    anomaly_codes = {x["code"] for x in result["anomalies"]}
    assert "REVERSE_SETTLEMENT" in codes
    assert "REVERSE_REWARD" in codes
    assert "settlement_posted_after_prior_cancellation" in anomaly_codes
    assert result["engine"] == "services/reconciler/app/engine.py"


def test_demo_sessions_are_isolated():
    first = create_session()
    second = create_session()

    create_order(first)
    first_state = get_state(first)
    second_state = get_state(second)

    assert first_state["order"] is not None
    assert second_state["order"] is None
    assert first_state["session_id"] != second_state["session_id"]


def test_order_lifecycle_requires_payment_before_confirmation():
    session_id = create_session()
    state = create_order(session_id)
    assert state["order"]["status"] == "HELD"
    assert state["order"]["payment_authorized"] is False

    response = client.post(f"/api/demo/sessions/{session_id}/confirm")
    assert response.status_code == 409

    paid = client.post(
        f"/api/demo/sessions/{session_id}/payment",
        json={"authorization_id": "auth-test"},
    )
    assert paid.status_code == 200
    assert paid.json()["state"]["order"]["payment_authorized"] is True

    confirmed = client.post(f"/api/demo/sessions/{session_id}/confirm")
    assert confirmed.status_code == 200
    assert confirmed.json()["state"]["order"]["status"] == "CONFIRMED"


def test_late_cancel_can_be_injected_reconciled_and_compensated():
    session_id = create_session()
    create_order(session_id)
    assert client.post(
        f"/api/demo/sessions/{session_id}/payment",
        json={"authorization_id": "auth-late"},
    ).status_code == 200
    assert client.post(f"/api/demo/sessions/{session_id}/confirm").status_code == 200

    cancelled = client.post(
        f"/api/demo/sessions/{session_id}/cancel",
        json={"delay_seconds": 52},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["event"]["received_at"] > cancelled.json()["event"]["occurred_at"]

    assert client.post(
        f"/api/demo/sessions/{session_id}/settlement",
        json={"amount": 9000},
    ).status_code == 200
    assert client.post(
        f"/api/demo/sessions/{session_id}/reward",
        json={"amount": 90},
    ).status_code == 200

    reconcile_response = client.post(f"/api/demo/sessions/{session_id}/reconcile")
    assert reconcile_response.status_code == 200
    reconciliation = reconcile_response.json()["reconciliation"]
    repair_codes = {item["code"] for item in reconciliation["repairs"]}
    anomaly_codes = {item["code"] for item in reconciliation["anomalies"]}
    assert {"REVERSE_SETTLEMENT", "REVERSE_REWARD"} <= repair_codes
    assert "settlement_posted_after_prior_cancellation" in anomaly_codes
    assert "reward_granted_after_prior_cancellation" in anomaly_codes

    applied_response = client.post(f"/api/demo/sessions/{session_id}/repairs/apply")
    assert applied_response.status_code == 200
    applied = set(applied_response.json()["applied"])
    assert {"REVERSE_SETTLEMENT", "REVERSE_REWARD"} <= applied

    state = applied_response.json()["state"]
    assert state["metrics"]["net_settlement"] == 0
    assert state["metrics"]["reward_balance"] == 0
    assert any(batch["posting_type"] == "REVERSE_SETTLEMENT" for batch in state["ledger_batches"])
    assert any(batch["posting_type"] == "REVERSE_REWARD" for batch in state["ledger_batches"])


def test_exact_redelivery_is_detected_without_manual_review():
    session_id = create_session()
    create_order(session_id)
    client.post(f"/api/demo/sessions/{session_id}/payment", json={"authorization_id": "auth-dup"})
    client.post(f"/api/demo/sessions/{session_id}/confirm")
    client.post(f"/api/demo/sessions/{session_id}/settlement", json={"amount": 9000})

    response = client.post(
        f"/api/demo/sessions/{session_id}/redelivery",
        json={"conflicting_amount": None},
    )
    assert response.status_code == 200
    state = response.json()["state"]
    repair_codes = {item["code"] for item in state["reconciliation"]["repairs"]}
    assert "NO_OP_DUPLICATE" in repair_codes
    assert "MANUAL_REVIEW" not in repair_codes


def test_conflicting_redelivery_goes_to_manual_review():
    session_id = create_session()
    create_order(session_id)
    client.post(f"/api/demo/sessions/{session_id}/payment", json={"authorization_id": "auth-conflict"})
    client.post(f"/api/demo/sessions/{session_id}/confirm")
    client.post(f"/api/demo/sessions/{session_id}/settlement", json={"amount": 9000})

    response = client.post(
        f"/api/demo/sessions/{session_id}/redelivery",
        json={"conflicting_amount": 13500},
    )
    assert response.status_code == 200
    state = response.json()["state"]
    anomaly_codes = {item["code"] for item in state["reconciliation"]["anomalies"]}
    repair_codes = {item["code"] for item in state["reconciliation"]["repairs"]}
    assert "conflicting_duplicate_payload" in anomaly_codes
    assert "MANUAL_REVIEW" in repair_codes


def test_capacity_drop_marks_confirmed_promise_at_risk():
    session_id = create_session()
    create_order(session_id, units=4, total=22000)
    client.post(f"/api/demo/sessions/{session_id}/payment", json={"authorization_id": "auth-cap"})
    client.post(f"/api/demo/sessions/{session_id}/confirm")

    response = client.post(
        f"/api/demo/sessions/{session_id}/capacity",
        json={"available_units": 2},
    )
    assert response.status_code == 200
    state = response.json()["state"]
    anomaly_codes = {item["code"] for item in state["reconciliation"]["anomalies"]}
    repair_codes = {item["code"] for item in state["reconciliation"]["repairs"]}
    assert "confirmed_promise_exceeds_revised_capacity" in anomaly_codes
    assert "RESLOT_REVIEW" in repair_codes

    applied = client.post(f"/api/demo/sessions/{session_id}/repairs/apply").json()
    assert "RESLOT_REVIEW" in applied["applied"]
    assert applied["state"]["order"]["status"] == "AT_RISK"


def test_capacity_risk_can_offer_and_accept_a_new_pickup_time():
    session_id = create_session()
    create_order(session_id, units=2, total=9000)
    assert client.post(
        f"/api/demo/sessions/{session_id}/payment",
        json={"authorization_id": "auth-promise"},
    ).status_code == 200
    assert client.post(f"/api/demo/sessions/{session_id}/confirm").status_code == 200

    revised = client.post(
        f"/api/demo/sessions/{session_id}/capacity",
        json={"available_units": 1},
    )
    assert revised.status_code == 200

    reconciled = client.post(f"/api/demo/sessions/{session_id}/reconcile")
    assert reconciled.status_code == 200
    assert "RESLOT_REVIEW" in {
        item["code"] for item in reconciled.json()["reconciliation"]["repairs"]
    }

    applied = client.post(f"/api/demo/sessions/{session_id}/repairs/apply")
    assert applied.status_code == 200
    state = applied.json()["state"]
    assert state["order"]["status"] == "AT_RISK"
    assert state["pickup_protection"] == {
        "status": "SUGGESTED",
        "original_pickup_at": "12:30",
        "suggested_pickup_at": "12:35",
    }

    accepted = client.post(
        f"/api/demo/sessions/{session_id}/pickup/reschedule",
        json={"pickup_at": "12:35"},
    )
    assert accepted.status_code == 200
    state = accepted.json()["state"]
    assert state["order"]["status"] == "CONFIRMED"
    assert state["order"]["pickup_at"] == "12:35"
    assert state["pickup_protection"]["status"] == "RESCHEDULED"
    assert any(event["event_type"] == "PickupRescheduled" for event in state["events"])
    assert "confirmed_promise_exceeds_revised_capacity" not in {
        item["code"] for item in state["reconciliation"]["anomalies"]
    }


def test_preset_load_populates_order_ledger_and_evidence():
    session_id = create_session()
    response = client.post(f"/api/demo/sessions/{session_id}/presets/late-cancel")
    assert response.status_code == 200
    state = response.json()
    assert state["order"]["order_id"] == "PP-1208"
    assert state["metrics"]["event_count"] >= 6
    assert state["metrics"]["ledger_batch_count"] == 2
    assert state["reconciliation"]["anomalies"]
    assert state["reconciliation"]["repairs"]


def test_reset_removes_order_and_business_state():
    session_id = create_session()
    create_order(session_id)
    response = client.post(f"/api/demo/sessions/{session_id}/reset")
    assert response.status_code == 200
    state = response.json()
    assert state["order"] is None
    assert state["events"] == []
    assert state["ledger_batches"] == []
    assert state["metrics"]["event_count"] == 0


def test_unknown_demo_session_returns_404():
    response = client.get("/api/demo/sessions/does-not-exist")
    assert response.status_code == 404


def test_normal_confirmed_order_can_post_settlement_and_reward():
    session_id = create_session()
    create_order(session_id, total=12000)
    assert client.post(
        f"/api/demo/sessions/{session_id}/payment",
        json={"authorization_id": "auth-normal"},
    ).status_code == 200
    assert client.post(f"/api/demo/sessions/{session_id}/confirm").status_code == 200

    settlement = client.post(
        f"/api/demo/sessions/{session_id}/settlement",
        json={"amount": 12000},
    )
    reward = client.post(
        f"/api/demo/sessions/{session_id}/reward",
        json={"amount": 120},
    )

    assert settlement.status_code == 200
    assert reward.status_code == 200
    state = reward.json()["state"]
    assert state["order"]["settled"] is True
    assert state["order"]["rewarded"] is True
    assert state["metrics"]["net_settlement"] == 12000
    assert state["metrics"]["reward_balance"] == 120
    assert [row["posting_type"] for row in state["ledger_batches"]] == [
        "SETTLEMENT",
        "REWARD",
    ]


def test_duplicate_preset_keeps_only_one_financial_side_effect():
    session_id = create_session()
    state = client.post(
        f"/api/demo/sessions/{session_id}/presets/duplicate"
    ).json()

    assert state["metrics"]["ledger_batch_count"] == 1
    assert state["metrics"]["net_settlement"] == 5500
    codes = {item["code"] for item in state["reconciliation"]["repairs"]}
    assert "NO_OP_DUPLICATE" in codes
    assert "MANUAL_REVIEW" not in codes


def test_conflict_preset_quarantines_meaning_without_second_financial_effect():
    session_id = create_session()
    state = client.post(
        f"/api/demo/sessions/{session_id}/presets/conflict"
    ).json()

    assert state["metrics"]["ledger_batch_count"] == 1
    assert state["metrics"]["net_settlement"] == 11500
    codes = {item["code"] for item in state["reconciliation"]["repairs"]}
    assert "MANUAL_REVIEW" in codes
