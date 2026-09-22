from datetime import UTC, datetime, timedelta

from app.engine import reconcile
from app.models import EventEnvelope, ReconcileRequest

BASE = datetime(2026, 9, 18, 3, 0, tzinfo=UTC)


def ev(event_id: str, event_type: str, occurred_s: int, received_s: int, payload=None):
    return EventEnvelope(
        event_id=event_id,
        aggregate_id="order-1",
        event_type=event_type,
        occurred_at=BASE + timedelta(seconds=occurred_s),
        received_at=BASE + timedelta(seconds=received_s),
        correlation_id="corr-1",
        payload=payload or {},
    )


def test_late_cancellation_triggers_financial_compensation_and_projection_rebuild():
    request = ReconcileRequest(events=[
        ev("hold", "PickupSlotHeld", 0, 0, {"capacity_units": 2}),
        ev("pay", "PaymentAuthorized", 1, 1),
        ev("confirm", "CommitmentConfirmed", 2, 2, {"capacity_units": 2}),
        ev("cancel", "CommitmentCancelled", 5, 50),
        ev("settle", "SettlementPosted", 10, 10, {"amount": "12000"}),
        ev("reward", "RewardGranted", 11, 11, {"amount": "1200"}),
    ])
    result = reconcile(request)
    assert "settlement_posted_after_prior_cancellation" in result.anomalies
    assert "reward_granted_after_prior_cancellation" in result.anomalies
    assert "REVERSE_SETTLEMENT" in result.repairs
    assert "REVERSE_REWARD" in result.repairs
    assert result.canonical_state.status == "CANCELLED"


def test_redelivery_is_deduplicated_without_manual_review():
    original = ev("settle", "SettlementPosted", 10, 10, {"amount": "12000"})
    redelivery = original.model_copy(update={"received_at": BASE + timedelta(seconds=15)})
    request = ReconcileRequest(events=[
        ev("hold", "PickupSlotHeld", 0, 0),
        ev("pay", "PaymentAuthorized", 1, 1),
        ev("confirm", "CommitmentConfirmed", 2, 2),
        original,
        redelivery,
    ])
    result = reconcile(request)
    assert result.duplicate_event_ids == ["settle"]
    assert "NO_OP_DUPLICATE" in result.repairs
    assert "MANUAL_REVIEW" not in result.repairs
    assert result.canonical_state.settlement_post_count == 1
    assert result.receive_order_state.settlement_post_count == 2


def test_conflicting_duplicate_requires_manual_review():
    first = ev("same", "RewardGranted", 10, 10, {"amount": "100"})
    second = first.model_copy(update={"payload": {"amount": "999"}, "received_at": BASE + timedelta(seconds=11)})
    result = reconcile(ReconcileRequest(events=[first, second]))
    assert "conflicting_duplicate_payload" in result.anomalies
    assert "MANUAL_REVIEW" in result.repairs


def test_capacity_revision_marks_confirmed_promise_at_risk():
    result = reconcile(ReconcileRequest(events=[
        ev("hold", "PickupSlotHeld", 0, 0, {"capacity_units": 3}),
        ev("pay", "PaymentAuthorized", 1, 1),
        ev("confirm", "CommitmentConfirmed", 2, 2, {"capacity_units": 3}),
        ev("capacity", "CapacityRevised", 3, 3, {"revision": 2, "available_units": 1}),
    ]))
    assert result.canonical_state.status == "AT_RISK"
    assert "RESLOT_REVIEW" in result.repairs


def test_pickup_reschedule_clears_capacity_risk():
    result = reconcile(ReconcileRequest(events=[
        ev("hold", "PickupSlotHeld", 0, 0, {"capacity_units": 2}),
        ev("pay", "PaymentAuthorized", 1, 1),
        ev("confirm", "CommitmentConfirmed", 2, 2, {"capacity_units": 2}),
        ev("capacity", "CapacityRevised", 3, 3, {"revision": 2, "available_units": 1}),
        ev("rescheduled", "PickupRescheduled", 4, 4, {"capacity_units": 2, "pickup_at": "12:35"}),
    ]))
    assert result.canonical_state.status == "CONFIRMED"
    assert "confirmed_promise_exceeds_revised_capacity" not in result.anomalies
    assert "RESLOT_REVIEW" not in result.repairs


def test_pickup_claim_moves_confirmed_order_to_picked_up():
    result = reconcile(ReconcileRequest(events=[
        ev("hold", "PickupSlotHeld", 0, 0, {"capacity_units": 1}),
        ev("pay", "PaymentAuthorized", 1, 1),
        ev("confirm", "CommitmentConfirmed", 2, 2, {"capacity_units": 1}),
        ev("claim", "PickupClaimed", 3, 3, {"pickup_code": "4821"}),
        ev("settle", "SettlementPosted", 4, 4, {"amount": "4500"}),
        ev("reward", "RewardGranted", 5, 5, {"amount": "45"}),
    ]))
    assert result.canonical_state.status == "PICKED_UP"
    assert result.canonical_state.settled is True
    assert result.canonical_state.rewarded is True
    assert "MANUAL_REVIEW" not in result.repairs


def test_receive_order_preserves_input_order_when_received_timestamps_tie():
    tied = BASE + timedelta(seconds=1)
    request = ReconcileRequest(events=[
        ev("hold", "PickupSlotHeld", 0, 0, {"capacity_units": 1}),
        ev("pay", "PaymentAuthorized", 1, 1),
        ev("confirm", "CommitmentConfirmed", 1, 1, {"capacity_units": 1}),
    ])
    request.events[1] = request.events[1].model_copy(update={"received_at": tied})
    request.events[2] = request.events[2].model_copy(update={"received_at": tied})

    result = reconcile(request)

    assert "confirmed_without_payment_authorization" not in result.anomalies
    assert "MANUAL_REVIEW" not in result.repairs
    assert result.receive_order_state.payment_authorized is True
    assert result.receive_order_state.status == "CONFIRMED"
