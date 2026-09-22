from __future__ import annotations

from collections import defaultdict

from .models import EventEnvelope, ReconcileRequest, ReconcileResult, Snapshot

_EVENT_PRIORITY = {
    "PickupSlotHeld": 10,
    "PaymentAuthorized": 20,
    "CommitmentConfirmed": 30,
    "CapacityRevised": 40,
    "PickupRescheduled": 45,
    "CommitmentCancelled": 50,
    "PickupClaimed": 55,
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
    original=request.events; unique,duplicate_ids,conflicting_duplicate=_unique_by_event_id(original)
    receive_order = [
        event
        for _, event in sorted(
            enumerate(original),
            key=lambda pair: (pair[1].received_at, pair[0]),
        )
    ]
    receive_state, receive_anomalies = _fold(receive_order, count_duplicates=True)
    canonical_order=sorted(unique,key=lambda e:(e.occurred_at,_EVENT_PRIORITY.get(e.event_type,999),e.event_id)); canonical_state,canonical_anomalies=_fold(canonical_order)
    anomalies=[]; evidence=set(); repairs=set()
    if duplicate_ids: anomalies.append("duplicate_event_delivery"); evidence.update(duplicate_ids); repairs.add("NO_OP_DUPLICATE")
    if conflicting_duplicate: anomalies.append("conflicting_duplicate_payload"); repairs.add("MANUAL_REVIEW")
    if receive_state != canonical_state: anomalies.append("receive_order_projection_drift"); repairs.add("REBUILD_PROJECTION")
    anomalies.extend(x for x in canonical_anomalies if x not in anomalies); anomalies.extend(x for x in receive_anomalies if x not in anomalies)
    cancels=[e for e in canonical_order if e.event_type=="CommitmentCancelled"]; settlements=[e for e in canonical_order if e.event_type=="SettlementPosted"]; rewards=[e for e in canonical_order if e.event_type=="RewardGranted"]
    if cancels:
        cancel=min(cancels,key=lambda e:e.occurred_at); bad_settlements=[e for e in settlements if cancel.occurred_at<=e.occurred_at]; bad_rewards=[e for e in rewards if cancel.occurred_at<=e.occurred_at]
        if bad_settlements and canonical_state.settled: anomalies.append("settlement_posted_after_prior_cancellation"); evidence.update([cancel.event_id,*[e.event_id for e in bad_settlements]]); repairs.add("REVERSE_SETTLEMENT")
        if bad_rewards and canonical_state.rewarded: anomalies.append("reward_granted_after_prior_cancellation"); evidence.update([cancel.event_id,*[e.event_id for e in bad_rewards]]); repairs.add("REVERSE_REWARD")
    if "confirmed_promise_exceeds_revised_capacity" in anomalies: repairs.add("RESLOT_REVIEW"); evidence.update(e.event_id for e in canonical_order if e.event_type=="CapacityRevised")
    if "confirmed_without_payment_authorization" in anomalies: repairs.add("MANUAL_REVIEW"); evidence.update(e.event_id for e in canonical_order if e.event_type=="CommitmentConfirmed")
    return ReconcileResult(aggregate_id=original[0].aggregate_id,receive_order_state=receive_state,canonical_state=canonical_state,anomalies=anomalies,repairs=[r for r in _REPAIR_ORDER if r in repairs],duplicate_event_ids=duplicate_ids,evidence_event_ids=sorted(evidence))
