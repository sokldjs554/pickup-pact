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
    assert all(item["capacity_units"] > 0 for store in stores for item in store["menu"])


def test_customer_can_query_capacity_aware_pickup_slots():
    response = client.get(
        "/api/demo/catalog/gangnam-pass-cafe/pickup-slots",
        params={"units": 3},
    )
    assert response.status_code == 200
    slots = response.json()
    assert len(slots) == 6
    assert all(slot["pickup_at"] and len(slot["pickup_at"]) == 5 for slot in slots)
    assert all(slot["capacity_units"] == 12 for slot in slots)
    assert all(
        slot["available_units"] == slot["capacity_units"] - slot["reserved_units"]
        for slot in slots
    )
    assert any(slot["can_fit"] for slot in slots)
    assert any(not slot["can_fit"] for slot in slots)
    assert {slot["status"] for slot in slots} <= {"AVAILABLE", "LIMITED", "FULL"}


def test_pickup_slot_query_rejects_unknown_store():
    response = client.get("/api/demo/catalog/not-a-store/pickup-slots")
    assert response.status_code == 404


def test_customer_pickup_quote_calculates_units_and_price_on_server():
    response = client.post(
        "/api/demo/catalog/gangnam-pass-cafe/pickup-quote",
        json={
            "line_items": [
                {"sku": "americano", "quantity": 2},
                {"sku": "cafe-latte", "quantity": 1},
            ]
        },
    )

    assert response.status_code == 200
    quote = response.json()
    assert quote["units"] == 4
    assert quote["total"] == 14000
    assert quote["items"] == "아메리카노 2개, 카페라떼"
    assert len(quote["slots"]) == 6


def test_customer_order_ignores_tampered_client_totals_when_line_items_exist():
    quote = client.post(
        "/api/demo/catalog/gangnam-pass-cafe/pickup-quote",
        json={"line_items": [{"sku": "americano", "quantity": 2}]},
    ).json()
    pickup_at = next(slot["pickup_at"] for slot in quote["slots"] if slot["can_fit"])
    session_id = client.post("/api/demo/sessions").json()["session_id"]

    response = client.post(
        f"/api/demo/sessions/{session_id}/orders",
        json={
            "store": "패스카페 강남역점",
            "store_id": "gangnam-pass-cafe",
            "line_items": [{"sku": "americano", "quantity": 2}],
            "items": "조작된 메뉴",
            "total": 1,
            "units": 1,
            "pickup_at": pickup_at,
        },
    )

    assert response.status_code == 200
    order = response.json()["state"]["order"]
    assert order["items"] == "아메리카노 2개"
    assert order["total"] == 9000
    assert order["units"] == 2


def test_customer_order_api_rechecks_selected_slot_capacity():
    slots = client.get(
        "/api/demo/catalog/gangnam-pass-cafe/pickup-slots",
        params={"units": 3},
    ).json()
    blocked = next(slot for slot in slots if not slot["can_fit"])
    session_id = client.post("/api/demo/sessions").json()["session_id"]

    response = client.post(
        f"/api/demo/sessions/{session_id}/orders",
        json={
            "store": "패스카페 강남역점",
            "store_id": "gangnam-pass-cafe",
            "items": "아메리카노 3개",
            "total": 13500,
            "pickup_at": blocked["pickup_at"],
            "units": 3,
        },
    )

    assert response.status_code == 409
    assert "no longer has enough capacity" in response.json()["detail"]



def test_merchant_delivery_reconnect_and_jit_early_ready_flow():
    session_id = create_session()
    create_order(session_id, units=2, total=9000)
    client.post(
        f"/api/demo/sessions/{session_id}/payment",
        json={"authorization_id": "merchant-demo"},
    )
    client.post(f"/api/demo/sessions/{session_id}/confirm")

    merchant = client.get(f"/api/demo/sessions/{session_id}/merchant")
    assert merchant.status_code == 200
    assert merchant.json()["status"] == "RECEIVED"
    assert merchant.json()["delivery_acknowledged"] is False
    assert merchant.json()["effects"] == ["NEW_ORDER_NOTIFICATION", "POS_PRINT"]

    redelivered = client.post(f"/api/demo/sessions/{session_id}/merchant/redeliver")
    assert redelivered.status_code == 200
    merchant = redelivered.json()["merchant_fulfillment"]
    assert merchant["redelivery_count"] == 1
    assert merchant["effects"] == ["NEW_ORDER_NOTIFICATION", "POS_PRINT"]

    acked = client.post(f"/api/demo/sessions/{session_id}/merchant/ack")
    assert acked.status_code == 200
    assert acked.json()["merchant_fulfillment"]["delivery_acknowledged"] is True

    accepted = client.post(f"/api/demo/sessions/{session_id}/merchant/accept")
    assert accepted.status_code == 200
    assert accepted.json()["merchant_fulfillment"]["status"] == "ACCEPTED"

    too_early = client.post(
        f"/api/demo/sessions/{session_id}/merchant/start",
        json={"timing": "TOO_EARLY"},
    )
    assert too_early.status_code == 409

    started = client.post(
        f"/api/demo/sessions/{session_id}/merchant/start",
        json={"timing": "ON_TIME"},
    )
    assert started.status_code == 200
    assert started.json()["merchant_fulfillment"]["status"] == "PREPARING"

    ready = client.post(
        f"/api/demo/sessions/{session_id}/merchant/ready",
        json={"timing": "EARLY"},
    )
    assert ready.status_code == 200
    merchant = ready.json()["merchant_fulfillment"]
    assert merchant["status"] == "READY"
    assert merchant["ready_quality"] == "EARLY"
    assert "READY_TOO_EARLY" in merchant["anomalies"]


def test_merchant_late_ready_auto_compensates_pickup_pact_in_points():
    session_id = create_session()
    create_order(session_id, units=1, total=4500)
    client.post(
        f"/api/demo/sessions/{session_id}/payment",
        json={"authorization_id": "merchant-late"},
    )
    client.post(f"/api/demo/sessions/{session_id}/confirm")
    client.post(f"/api/demo/sessions/{session_id}/merchant/accept")
    client.post(
        f"/api/demo/sessions/{session_id}/merchant/start",
        json={"timing": "ON_TIME"},
    )

    ready = client.post(
        f"/api/demo/sessions/{session_id}/merchant/ready",
        json={"timing": "LATE"},
    )
    assert ready.status_code == 200
    state = ready.json()
    assert state["merchant_fulfillment"]["ready_quality"] == "LATE"
    assert "READY_LATE" in state["merchant_fulfillment"]["anomalies"]
    assert state["pickup_pact"]["status"] == "COMPENSATED"
    reward_batches = [
        row for row in state["ledger_batches"]
        if row["posting_type"] == "REWARD"
    ]
    assert reward_batches[-1]["amount"] == 500
    assert reward_batches[-1]["currency"] == "PTS"


def test_customer_cancel_before_merchant_preparation_is_authoritatively_approved():
    session_id = create_session()
    create_order(session_id, units=2, total=9000)
    client.post(
        f"/api/demo/sessions/{session_id}/payment",
        json={"authorization_id": "merchant-cancel-before"},
    )
    client.post(f"/api/demo/sessions/{session_id}/confirm")

    cancelled = client.post(
        f"/api/demo/sessions/{session_id}/cancel",
        json={"delay_seconds": 0},
    )
    assert cancelled.status_code == 200
    body = cancelled.json()
    assert body["decision"] == "APPROVED"
    assert body["request_event"]["event_type"] == "CancellationRequested"
    assert body["event"]["event_type"] == "CommitmentCancelled"
    assert body["state"]["order"]["status"] == "CANCELLED"
    assert body["state"]["merchant_fulfillment"]["status"] == "CANCELLED"
    assert body["state"]["capacity"]["reserved_units"] == 0


def test_customer_cancel_after_merchant_preparation_is_rejected_without_state_split():
    session_id = create_session()
    create_order(session_id, units=2, total=9000)
    client.post(
        f"/api/demo/sessions/{session_id}/payment",
        json={"authorization_id": "merchant-cancel"},
    )
    client.post(f"/api/demo/sessions/{session_id}/confirm")
    client.post(f"/api/demo/sessions/{session_id}/merchant/accept")
    client.post(
        f"/api/demo/sessions/{session_id}/merchant/start",
        json={"timing": "ON_TIME"},
    )

    cancelled = client.post(
        f"/api/demo/sessions/{session_id}/cancel",
        json={"delay_seconds": 0},
    )
    assert cancelled.status_code == 200
    body = cancelled.json()
    assert body["decision"] == "REJECTED"
    assert body["event"]["event_type"] == "CancellationRejected"
    assert body["state"]["order"]["status"] == "CONFIRMED"
    merchant = body["state"]["merchant_fulfillment"]
    assert merchant["status"] == "PREPARING"
    assert "CANCEL_AFTER_PREPARATION" in merchant["anomalies"]
    assert body["state"]["capacity"]["reserved_units"] == 2
    assert body["state"]["pickup_pact"]["status"] == "ACTIVE"


def test_landing_page_exposes_guided_and_expert_layers():
    response = client.get("/classic")
    assert response.status_code == 200
    body = response.text
    for label in [
        "오늘 뭐 드실래요?",
        "근처 매장",
        "장바구니 보기",
        "내 주문",
        "체험 손님",
        "메뉴 보기",
        "주문 취소",
        "주문 내역",
        "모바일 영수증",
        "커피 받을 때 보여줄 번호",
        "주문 처리 내역",
        "수령 완료 체험",
        "Pickup Pact — 픽업 시간을 약속해요.",
        "체험 포인트 500P",
        "픽업 시간 선택",
        "괜찮아요",
        "주문 흐름",
        "매장 처리량",
        "장애 주입",
        "정합성 복구",
        "정산 · 감사",
        "기록 다시 확인하기",
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


def test_pickup_pact_is_issued_renegotiated_and_auto_compensated():
    session_id = create_session()
    create_order(session_id, units=2, total=9000)
    client.post(
        f"/api/demo/sessions/{session_id}/payment",
        json={"authorization_id": "auth-pact"},
    )
    confirmed = client.post(f"/api/demo/sessions/{session_id}/confirm")
    assert confirmed.status_code == 200
    state = confirmed.json()["state"]
    pact = state["pickup_pact"]
    assert pact["status"] == "ACTIVE"
    assert pact["version"] == 1
    assert pact["compensation_points"] == 500
    assert any(event["event_type"] == "PickupPactIssued" for event in state["events"])

    client.post(
        f"/api/demo/sessions/{session_id}/capacity",
        json={"available_units": 1},
    )
    client.post(f"/api/demo/sessions/{session_id}/reconcile")
    client.post(f"/api/demo/sessions/{session_id}/repairs/apply")
    state = get_state(session_id)
    suggested = state["pickup_protection"]["suggested_pickup_at"]
    accepted = client.post(
        f"/api/demo/sessions/{session_id}/pickup/reschedule",
        json={"pickup_at": suggested},
    )
    assert accepted.status_code == 200
    state = accepted.json()["state"]
    pact = state["pickup_pact"]
    assert pact["status"] == "ACTIVE"
    assert pact["version"] == 2
    assert pact["promised_at"] == suggested
    assert any(event["event_type"] == "PickupPactRenegotiated" for event in state["events"])

    breached = client.post(f"/api/demo/sessions/{session_id}/pickup/pact/breach")
    assert breached.status_code == 200
    state = breached.json()["state"]
    assert state["pickup_pact"]["status"] == "COMPENSATED"
    assert state["pickup_pact"]["compensation_granted"] is True
    assert state["metrics"]["reward_balance"] == 500
    assert any(event["event_type"] == "PickupPactBreached" for event in state["events"])
    assert any(
        event["event_type"] == "RewardGranted"
        and event["payload"].get("source") == "pickup_pact_breach"
        for event in state["events"]
    )

    duplicate = client.post(f"/api/demo/sessions/{session_id}/pickup/pact/breach")
    assert duplicate.status_code == 409


def test_pickup_code_is_one_time_and_creates_trust_receipt_history():
    session_id = create_session()
    create_order(session_id, units=1, total=4500)
    assert client.post(
        f"/api/demo/sessions/{session_id}/payment",
        json={"authorization_id": "auth-pickup"},
    ).status_code == 200
    confirmed = client.post(f"/api/demo/sessions/{session_id}/confirm")
    assert confirmed.status_code == 200
    code = confirmed.json()["state"]["order"]["pickup_code"]
    assert code and len(code) == 4 and code.isdigit()

    claimed = client.post(
        f"/api/demo/sessions/{session_id}/pickup/claim",
        json={"pickup_code": code},
    )
    assert claimed.status_code == 200
    state = claimed.json()["state"]
    assert state["order"]["status"] == "PICKED_UP"
    assert state["order"]["pickup_claimed"] is True
    assert state["metrics"]["net_settlement"] == 4500
    assert state["metrics"]["reward_balance"] == 45
    assert any(event["event_type"] == "PickupClaimed" for event in state["events"])

    reused = client.post(
        f"/api/demo/sessions/{session_id}/pickup/claim",
        json={"pickup_code": code},
    )
    assert reused.status_code == 409

    history = client.get(f"/api/demo/sessions/{session_id}/customer/history")
    assert history.status_code == 200
    assert len(history.json()) == 1
    assert history.json()[0]["status"] == "PICKED_UP"

    receipt = client.get(
        f"/api/demo/sessions/{session_id}/customer/receipts/{state['order']['order_id']}"
    )
    assert receipt.status_code == 200
    payload = receipt.json()
    assert payload["final_charge"] == 4500
    assert payload["reward_balance"] == 45
    assert any(item["type"] == "PickupClaimed" for item in payload["timeline"])


def test_cancelled_order_is_archived_with_zero_final_charge():
    session_id = create_session()
    create_order(session_id, units=2, total=9000)
    client.post(
        f"/api/demo/sessions/{session_id}/payment",
        json={"authorization_id": "auth-cancel-history"},
    )
    client.post(f"/api/demo/sessions/{session_id}/confirm")
    client.post(
        f"/api/demo/sessions/{session_id}/cancel",
        json={"delay_seconds": 52},
    )
    client.post(
        f"/api/demo/sessions/{session_id}/settlement",
        json={"amount": 9000},
    )
    client.post(
        f"/api/demo/sessions/{session_id}/reward",
        json={"amount": 90},
    )
    client.post(f"/api/demo/sessions/{session_id}/reconcile")
    repaired = client.post(f"/api/demo/sessions/{session_id}/repairs/apply")
    assert repaired.status_code == 200

    history = client.get(f"/api/demo/sessions/{session_id}/customer/history").json()
    assert len(history) == 1
    assert history[0]["status"] == "CANCELLED"
    assert history[0]["final_charge"] == 0
    receipt = history[0]["receipt"]
    assert receipt["settlement_balance"] == 0
    assert receipt["reward_balance"] == 0
    assert any(item["type"] == "SettlementReversed" for item in receipt["timeline"])
    assert any(item["type"] == "RewardReversed" for item in receipt["timeline"])


def test_next_customer_order_preserves_terminal_history():
    session_id = create_session()
    create_order(session_id, units=1, total=4500)
    client.post(
        f"/api/demo/sessions/{session_id}/payment",
        json={"authorization_id": "auth-next"},
    )
    confirmed = client.post(f"/api/demo/sessions/{session_id}/confirm").json()
    code = confirmed["state"]["order"]["pickup_code"]
    client.post(
        f"/api/demo/sessions/{session_id}/pickup/claim",
        json={"pickup_code": code},
    )

    next_order = client.post(f"/api/demo/sessions/{session_id}/customer/next-order")
    assert next_order.status_code == 200
    state = next_order.json()
    assert state["order"] is None
    assert len(state["customer_history"]) == 1
    assert state["customer_history"][0]["status"] == "PICKED_UP"


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
