from __future__ import annotations

from collections import defaultdict
import hashlib
import json

from .models import EventEnvelope, ReconcileRequest, ReconcileResult, Snapshot
from .evidence_order import causal_order
from .financial_scope import accounting_amount, plan_financial_repairs

_EVENT_PRIORITY = {
    "PickupSlotHeld": 10,
    "PaymentAuthorized": 20,
    "CommitmentConfirmed": 30,
    "CancellationRequested": 31,
    "CancellationRejected": 32,
    "PickupPactIssued": 35,
    "CapacityRevised": 40,
    "PickupRescheduled": 45,
    "PickupPactRenegotiated": 46,
    "PickupPactBreached": 50,
    "CommitmentCancelled": 55,
    "PartialCancellationApplied": 56,
    "PickupClaimed": 60,
    "SettlementPosted": 60,
    "RewardGranted": 70,
    "SettlementReversed": 80,
    "RewardReversed": 90,
}

_REPAIR_ORDER = ["NO_OP_DUPLICATE","REBUILD_PROJECTION","REVERSE_SETTLEMENT","REVERSE_REWARD","RESLOT_REVIEW","MANUAL_REVIEW"]


def _semantic_event_fingerprint(event: EventEnvelope) -> str:
    payload = event.model_dump(mode="json")
    payload.pop("received_at", None)
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


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
    settlement_balances: dict[str, int] = {}
    reward_balances: dict[str, int] = {}

    def posting_amount(event: EventEnvelope) -> int:
        try:
            return accounting_amount(event.payload.get("amount"))
        except ValueError:
            # Keep malformed postings visibly open; the financial planner will
            # quarantine them instead of allowing a false "fully reversed" fold.
            return 1

    def apply_reversal(event: EventEnvelope, balances: dict[str, int]) -> None:
        target = event.payload.get("source_event_id") or event.causation_id
        if not target or target not in balances:
            return
        try:
            amount = accounting_amount(event.payload.get("amount")) if "amount" in event.payload else balances[target]
        except ValueError:
            return
        balances[target] = max(0, balances[target] - amount)

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
        elif event.event_type == "SettlementPosted":
            state.settlement_post_count+=1
            settlement_balances[event.event_id]=posting_amount(event)
        elif event.event_type == "RewardGranted":
            state.reward_post_count+=1
            reward_balances[event.event_id]=posting_amount(event)
        elif event.event_type == "SettlementReversed":
            apply_reversal(event, settlement_balances)
        elif event.event_type == "RewardReversed":
            apply_reversal(event, reward_balances)

    state.settled = any(amount > 0 for amount in settlement_balances.values())
    state.rewarded = any(amount > 0 for amount in reward_balances.values())
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

    full_cancels = [e for e in unique if e.event_type == "CommitmentCancelled"]
    partial_cancels = [e for e in unique if e.event_type == "PartialCancellationApplied"]
    financial_cancels = full_cancels + partial_cancels
    claims = [e for e in unique if e.event_type == "PickupClaimed"]
    settlements = [e for e in unique if e.event_type == "SettlementPosted"]
    rewards = [e for e in unique if e.event_type == "RewardGranted"]
    financial_actions = []

    if full_cancels and claims:
        # Only a full committed cancellation conflicts with final pickup.
        # Partial financial adjustment does not terminalize the order.
        manual.append("conflicting_terminal_facts")
        evidence.update(e.event_id for e in full_cancels + claims)

    # Preserve an explicit ambiguity guard for unrelated financial facts with an
    # identical business timestamp. Causal evidence, when present, resolves it.
    for cancel in financial_cancels:
        for posting in settlements + rewards:
            if (
                not ordered.precedes(cancel.event_id, posting.event_id)
                and not ordered.precedes(posting.event_id, cancel.event_id)
                and cancel.occurred_at == posting.occurred_at
            ):
                manual.append("ambiguous_financial_order")
                evidence.update((cancel.event_id, posting.event_id))

    if financial_cancels:
        financial_actions, financial_blockers = plan_financial_repairs(unique)
        manual.extend(financial_blockers)
        if financial_blockers:
            evidence.update(event.event_id for event in unique)
        if financial_actions:
            evidence.update(event.event_id for event in financial_cancels)
            evidence.update(action.target_event_id for action in financial_actions)
            posting_by_id = {event.event_id: event for event in settlements + rewards}
            for action in financial_actions:
                repairs.add(action.repair)
                posting = posting_by_id.get(action.target_event_id)
                prior_cancel = bool(posting) and any(
                    ordered.precedes(cancel.event_id, posting.event_id)
                    or (
                        not ordered.precedes(posting.event_id, cancel.event_id)
                        and cancel.occurred_at < posting.occurred_at
                    )
                    for cancel in financial_cancels
                )
                if action.repair == "REVERSE_SETTLEMENT":
                    anomaly = (
                        "settlement_posted_after_prior_cancellation"
                        if prior_cancel else "cancelled_order_has_open_settlement"
                    )
                else:
                    anomaly = (
                        "reward_granted_after_prior_cancellation"
                        if prior_cancel else "cancelled_order_has_open_order_reward"
                    )
                if anomaly not in anomalies:
                    anomalies.append(anomaly)

    if "confirmed_promise_exceeds_revised_capacity" in canonical_anomalies:
        repairs.add("RESLOT_REVIEW")
        evidence.update(e.event_id for e in unique if e.event_type == "CapacityRevised")
    if "confirmed_without_payment_authorization" in canonical_anomalies:
        manual.append("confirmed_without_payment_authorization")
        evidence.update(e.event_id for e in unique if e.event_type == "CommitmentConfirmed")

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
        financial_actions = []
        canonical_state = canonical_state.model_copy(update={"status": "UNRESOLVED"})

    return ReconcileResult(
        aggregate_id=original[0].aggregate_id,
        receive_order_state=receive_state,
        canonical_state=canonical_state,
        anomalies=anomalies,
        repairs=[repair for repair in _REPAIR_ORDER if repair in repairs],
        duplicate_event_ids=duplicate_ids,
        evidence_event_ids=sorted(evidence),
        financial_actions=financial_actions,
        source_event_ids=sorted({event.event_id for event in unique}),
        source_event_fingerprints={
            event.event_id: _semantic_event_fingerprint(event)
            for event in sorted(unique, key=lambda row: row.event_id)
        },
        decision=decision,
        blocking_reasons=blockers,
        missing_event_ids=ordered.missing_ids,
    )
