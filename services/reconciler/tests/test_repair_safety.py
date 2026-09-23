"""Counterexamples missing from the original green suite.

These exercise the production reconcile() function, not a replacement model.
Cancellation is a committed fact here; cancellation *requests* cannot authorize
financial repair. All clocks and event IDs are deterministic.
"""
from datetime import UTC, datetime, timedelta
from itertools import permutations

import pytest

from app.engine import reconcile
from app.models import EventEnvelope, ReconcileRequest

T0 = datetime(2026, 9, 23, tzinfo=UTC)
MUTATING = {"REVERSE_SETTLEMENT", "REVERSE_REWARD", "REBUILD_PROJECTION"}


def event(key, kind, second, *, arrived=None, cause=None, payload=None):
    return EventEnvelope(
        event_id=key, aggregate_id="audit-order", event_type=kind,
        occurred_at=T0 + timedelta(seconds=second),
        received_at=T0 + timedelta(seconds=second if arrived is None else arrived),
        causation_id=cause, payload=payload or {},
    )


def prefix():
    return [event("hold", "PickupSlotHeld", 0),
            event("pay", "PaymentAuthorized", 1, cause="hold"),
            event("confirmed", "CommitmentConfirmed", 2, cause="pay")]


def cancelled():
    return prefix() + [event("cancel", "CommitmentCancelled", 3, cause="confirmed")]


def decision(result):
    # The unmodified production version has no explicit decision field.
    return getattr(result, "decision", "AUTO")


@pytest.mark.parametrize("reverse_delivery", [False, True])
def test_conflicting_money_event_quarantines_every_automatic_repair(reverse_delivery):
    left = event("posting", "SettlementPosted", 4, payload={"amount": "9000"})
    right = left.model_copy(update={"payload": {"amount": "1"},
                                    "received_at": T0 + timedelta(seconds=5)})
    copies = [right, left] if reverse_delivery else [left, right]
    result = reconcile(ReconcileRequest(events=cancelled() + copies))
    assert not (MUTATING & set(result.repairs)), result.model_dump()
    assert decision(result) == "MANUAL_REVIEW"
    assert "posting" in result.evidence_event_ids


@pytest.mark.parametrize("claim_at,cancel_at", [(4, 3), (3, 4)])
def test_conflicting_cancelled_and_picked_up_facts_do_not_choose_a_winner_by_clock(claim_at, cancel_at):
    result = reconcile(ReconcileRequest(events=prefix() + [
        event("cancel", "CommitmentCancelled", cancel_at),
        event("claimed", "PickupClaimed", claim_at),
        event("posting", "SettlementPosted", 5, payload={"amount": "9000"}),
    ]))
    assert decision(result) == "MANUAL_REVIEW", result.model_dump()
    assert not (MUTATING & set(result.repairs))
    assert result.canonical_state.status == "UNRESOLVED"


def test_missing_cause_waits_and_replay_after_cause_arrives_resolves():
    events = cancelled() + [event("posting", "SettlementPosted", 5, cause="late-evidence")]
    incomplete = reconcile(ReconcileRequest(events=events))
    assert decision(incomplete) == "WAIT_FOR_EVIDENCE", incomplete.model_dump()
    assert not (MUTATING & set(incomplete.repairs))
    complete = reconcile(ReconcileRequest(events=events + [
        event("late-evidence", "CapacityRevised", 4, arrived=20,
              cause="cancel", payload={"revision": 1})
    ]))
    assert decision(complete) == "AUTO"
    assert "REVERSE_SETTLEMENT" in complete.repairs


def test_explicit_causation_wins_over_skewed_event_timestamps():
    result = reconcile(ReconcileRequest(events=cancelled() + [
        event("posting", "SettlementPosted", 1, arrived=10, cause="cancel")
    ]))
    assert "REVERSE_SETTLEMENT" in result.repairs, result.model_dump()
    assert decision(result) == "AUTO"
    assert "event_time_causality_conflict" in result.anomalies


def test_causal_cycle_is_quarantined_without_fabricating_order():
    result = reconcile(ReconcileRequest(events=prefix() + [
        event("cancel", "CommitmentCancelled", 3, cause="posting"),
        event("posting", "SettlementPosted", 4, cause="cancel"),
    ]))
    assert decision(result) == "MANUAL_REVIEW", result.model_dump()
    assert not (MUTATING & set(result.repairs))
    assert "causal_cycle" in result.anomalies


def test_tied_financial_timestamps_without_causal_evidence_are_not_ordered_by_type():
    result = reconcile(ReconcileRequest(events=cancelled() + [
        event("posting", "SettlementPosted", 3)
    ]))
    assert decision(result) == "MANUAL_REVIEW", result.model_dump()
    assert not (MUTATING & set(result.repairs))


def test_invalid_confirmation_does_not_combine_review_with_financial_repair():
    result = reconcile(ReconcileRequest(events=[
        event("hold", "PickupSlotHeld", 0),
        event("confirmed", "CommitmentConfirmed", 2),
        event("cancel", "CommitmentCancelled", 3),
        event("posting", "SettlementPosted", 4),
    ]))
    assert "MANUAL_REVIEW" in result.repairs
    assert not (MUTATING & set(result.repairs)), result.model_dump()


def test_out_of_order_delivery_is_not_a_canonical_payment_failure():
    events = prefix()
    events[1] = events[1].model_copy(update={"received_at": T0 + timedelta(seconds=30)})
    result = reconcile(ReconcileRequest(events=events))
    assert decision(result) == "AUTO"
    assert "MANUAL_REVIEW" not in result.repairs, result.model_dump()


def test_normal_late_cancellation_still_proposes_required_reversals():
    events = cancelled() + [
        event("settle", "SettlementPosted", 4, cause="cancel"),
        event("reward", "RewardGranted", 5, cause="cancel"),
    ]
    result = reconcile(ReconcileRequest(events=events))
    assert decision(result) == "AUTO"
    assert {"REVERSE_SETTLEMENT", "REVERSE_REWARD"} <= set(result.repairs)


def test_exact_redelivery_does_not_quarantine_normal_order():
    events = prefix()
    duplicate = events[-1].model_copy(update={"received_at": T0 + timedelta(seconds=40)})
    result = reconcile(ReconcileRequest(events=events + [duplicate]))
    assert decision(result) == "AUTO"
    assert result.canonical_state.status == "CONFIRMED"
    assert "MANUAL_REVIEW" not in result.repairs


def test_input_permutation_does_not_change_safe_canonical_decision():
    root = prefix()
    suffix = [event("cancel", "CommitmentCancelled", 3, cause="confirmed"),
              event("posting", "SettlementPosted", 4, cause="cancel")]
    decisions = set()
    for variant in permutations(root + suffix):
        result = reconcile(ReconcileRequest(events=list(variant)))
        decisions.add((decision(result), result.canonical_state.status,
                       tuple(sorted(set(result.repairs) & {"REVERSE_SETTLEMENT", "REVERSE_REWARD"}))))
    assert decisions == {("AUTO", "CANCELLED", ("REVERSE_SETTLEMENT",))}
