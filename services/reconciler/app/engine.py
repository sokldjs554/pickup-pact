from __future__ import annotations

from collections import defaultdict

from .models import EventEnvelope, ReconcileRequest, ReconcileResult, Snapshot
from .evidence_order import causal_order
from .financial_scope import scope_blockers

_EVENT_PRIORITY = {
    "PickupSlotHeld": 10,
    "PaymentAuthorized": 20,
    "CommitmentConfirmed": 30,
    "PickupPactIssued": 35,
    "CapacityRevised": 40,
    "PickupRescheduled": 45,
    "PickupPactRenegotiated": 46,
    "PickupPactBreached": 50,
    "CommitmentCancelled": 55,
    "PickupClaimed": 60,
    "SettlementPosted": 60,
    "RewardGranted": 70,
    "SettlementReversed": 80,
    "RewardReversed": 90,
}

_REPAIR_ORDER = ["NO_OP_DUPLICATE","REBUILD_PROJECTION","REVERSE_SETTLEMENT","REVERSE_REWARD","RESLOT_REVIEW","MANUAL_REVIEW"]


def _unique_by_event_id(events: list[EventEnvelope]) -> tuple[list[EventEnvelope], list[str], bool]:
    grouped: dict[str, list[EventEnvelope]] = defaultdict(list)
    for event in events:
        grouped[event.event_id].append(event)
    duplicates = sorted(event_id for event_id, copies in grouped.items() if len(copies) > 1)
    conflicting = False
    unique: list[EventEnvelope] = []
    for copies in grouped.values():
        chosen = min(copies, key=lambda e: (e.received_at, e.occurred_at, e.event_type))
        def semantic_payload(event: EventEnvelope) -> dict:
            data = event.model_dump(mode="json"); data.pop("received_at", None); return data
        canonical_payload = semantic_payload(chosen)
        if any(semantic_payload(copy) != canonical_payload for copy in copies): conflicting = True
        unique.append(chosen)
    return unique, duplicates, conflicting


def _fold(events: list[EventEnvelope], *, count_duplicates: bool = False) -> tuple[Snapshot, list[str]]:
    state = Snapshot(); anomalies: list[str] = []; seen: set[str] = set(); required_capacity = 1; capacity_at_risk = False
    for event in events:
        if not count_duplicates and event.event_id in seen: continue
        seen.add(event.event_id)
        if event.event_type == "PickupSlotHeld":
            state.status="HELD"; required_capacity=int(event.payload.get("capacity_units", required_capacity))
        elif event.event_type == "PaymentAuthorized":
            state.payment_authorized=True
        elif event.event_type == "CommitmentConfirmed":
            if not state.payment_authorized: anomalies.append("confirmed_without_payment_authorization")
            state.status="CONFIRMED"; required_capacity=int(event.payload.get("capacity_units", required_capacity)); capacity_at_risk=False
        elif event.event_type == "CapacityRevised":
            state.capacity_revision=int(event.payload.get("revision", state.capacity_revision+1)); available=event.payload.get("available_units")
            if state.status=="CONFIRMED" and available is not None and int(available)<required_capacity:
                state.status="AT_RISK"; capacity_at_risk=True
        elif event.event_type == "PickupRescheduled":
            state.status="CONFIRMED"; required_capacity=int(event.payload.get("capacity_units", required_capacity)); capacity_at_risk=False
        elif event.event_type == "CommitmentCancelled":
            state.status="CANCELLED"; capacity_at_risk=False
        elif event.event_type == "PickupClaimed":
            state.status="PICKED_UP"; capacity_at_risk=False
        elif event.event_type == "SettlementPosted": state.settlement_post_count+=1; state.settled=True
        elif event.event_type == "RewardGranted": state.reward_post_count+=1; state.rewarded=True
        elif event.event_type == "SettlementReversed": state.settled=False
        elif event.event_type == "RewardReversed": state.rewarded=False
    if capacity_at_risk:
        anomalies.append("confirmed_promise_exceeds_revised_capacity")
    return state, anomalies


def reconcile(request: ReconcileRequest) -> ReconcileResult:
    original = request.events
    unique, duplicate_ids, conflicting_duplicate = _unique_by_event_id(original)
    receive_order = [
        event for _, event in sorted(
            enumerate(original), key=lambda pair: (pair[1].received_at, pair[0])
        )
    ]
    # Retained for compatibility as a deliberately naive diagnostic projection.
    # It is NOT a benchmark baseline and never authorizes a financial repair.
    receive_state, _ = _fold(receive_order, count_duplicates=True)
    ordered = causal_order(
        unique,
        key=lambda e: (e.occurred_at, _EVENT_PRIORITY.get(e.event_type, 999), e.event_id),
    )
    canonical_state, canonical_anomalies = _fold(ordered.events)
    anomalies: list[str] = []
    evidence: set[str] = set()
    repairs: set[str] = set()
    manual: list[str] = []

    if duplicate_ids:
        anomalies.append("duplicate_event_delivery")
        evidence.update(duplicate_ids)
        repairs.add("NO_OP_DUPLICATE")
    if conflicting_duplicate:
        manual.append("conflicting_duplicate_payload")
    if ordered.cyclic_ids:
        manual.append("causal_cycle")
        evidence.update(ordered.cyclic_ids)
    if ordered.missing_ids:
        anomalies.append("missing_causal_evidence")
        evidence.update(e.event_id for e in unique if e.causation_id in ordered.missing_ids)
    if ordered.clock_conflict_ids:
        anomalies.append("event_time_causality_conflict")
        evidence.update(ordered.clock_conflict_ids)
    if receive_state != canonical_state:
        anomalies.append("receive_order_projection_drift")
        repairs.add("REBUILD_PROJECTION")
    # A receive-time payment gap is not a canonical payment failure once the
    # authorization evidence is present and ordered before confirmation.
    anomalies.extend(x for x in canonical_anomalies if x not in anomalies)

    cancels = [e for e in unique if e.event_type == "CommitmentCancelled"]
    claims = [e for e in unique if e.event_type == "PickupClaimed"]
    settlements = [e for e in unique if e.event_type == "SettlementPosted"]
    rewards = [e for e in unique if e.event_type == "RewardGranted"]
    if cancels and claims:
        # Both are committed terminal facts. Timestamp order cannot arbitrate
        # an invalid cross-context transition; this requires authoritative review.
        manual.append("conflicting_terminal_facts")
        evidence.update(e.event_id for e in cancels + claims)

    def after_cancel(cancel: EventEnvelope, posting: EventEnvelope) -> bool:
        if ordered.precedes(cancel.event_id, posting.event_id):
            return True
        if ordered.precedes(posting.event_id, cancel.event_id):
            return False
        if cancel.occurred_at == posting.occurred_at:
            if "ambiguous_financial_order" not in manual:
                manual.append("ambiguous_financial_order")
            evidence.update((cancel.event_id, posting.event_id))
            return False
        # Legacy event-time policy for unrelated events: this assumes comparable
        # producer clocks. No arbitrary cross-system clock-order guarantee.
        return cancel.occurred_at < posting.occurred_at

    if cancels:
        for postings, active, anomaly, repair in (
            (settlements, canonical_state.settled,
             "settlement_posted_after_prior_cancellation", "REVERSE_SETTLEMENT"),
            (rewards, canonical_state.rewarded,
             "reward_granted_after_prior_cancellation", "REVERSE_REWARD"),
        ):
            invalid = [post for post in postings if any(after_cancel(c, post) for c in cancels)]
            if invalid and active:
                anomalies.append(anomaly)
                evidence.update(e.event_id for e in cancels + invalid)
                repairs.add(repair)

    if "confirmed_promise_exceeds_revised_capacity" in canonical_anomalies:
        repairs.add("RESLOT_REVIEW")
        evidence.update(e.event_id for e in unique if e.event_type == "CapacityRevised")
    if "confirmed_without_payment_authorization" in canonical_anomalies:
        manual.append("confirmed_without_payment_authorization")
        evidence.update(e.event_id for e in unique if e.event_type == "CommitmentConfirmed")

    # A missing antecedent remains WAIT, not a guessed full-amount reversal.
    if not ordered.missing_ids:
        scope_errors = scope_blockers(unique, repairs)
        manual.extend(scope_errors)
        if scope_errors:
            evidence.update(e.event_id for e in unique)

    decision = "AUTO"
    blockers = list(dict.fromkeys(manual))
    if blockers:
        decision = "MANUAL_REVIEW"
    elif ordered.missing_ids:
        decision = "WAIT_FOR_EVIDENCE"
    if ordered.missing_ids:
        blockers.append("missing_causal_evidence")
    for blocker in blockers:
        if blocker not in anomalies:
            anomalies.append(blocker)

    if decision != "AUTO":
        # Fail closed: a manual/wait marker must NEVER coexist with executable
        # financial or projection-rebuild instructions. Preserve the old
        # MANUAL_REVIEW repair for legacy consumers; decision distinguishes wait.
        repairs.intersection_update({"NO_OP_DUPLICATE"})
        repairs.add("MANUAL_REVIEW")
        canonical_state = canonical_state.model_copy(update={"status": "UNRESOLVED"})

    return ReconcileResult(
        aggregate_id=original[0].aggregate_id,
        receive_order_state=receive_state,
        canonical_state=canonical_state,
        anomalies=anomalies,
        repairs=[repair for repair in _REPAIR_ORDER if repair in repairs],
        duplicate_event_ids=duplicate_ids,
        evidence_event_ids=sorted(evidence),
        decision=decision,
        blocking_reasons=blockers,
        missing_event_ids=ordered.missing_ids,
    )
