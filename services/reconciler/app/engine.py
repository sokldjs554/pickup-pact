from __future__ import annotations
from collections import defaultdict
from .models import DomainEvent, RepairCommand, ReconcileResponse

FINANCIAL = {"SETTLED", "REWARD_GRANTED"}

def reconcile(aggregate_id: str, events: list[DomainEvent]) -> ReconcileResponse:
    if any(e.aggregate_id != aggregate_id for e in events):
        raise ValueError("all events must belong to aggregate_id")

    canonical = sorted(events, key=lambda e: (e.occurred_at, e.received_at, e.event_id))
    by_id: dict[str, list[DomainEvent]] = defaultdict(list)
    for event in events:
        by_id[event.event_id].append(event)

    anomalies: list[str] = []
    commands: list[RepairCommand] = []
    duplicate_ids: list[str] = []

    for event_id, copies in by_id.items():
        if len(copies) < 2:
            continue
        fingerprints = {(e.type, e.amount, tuple(sorted(e.payload.items()))) for e in copies}
        duplicate_ids.append(event_id)
        if len(fingerprints) == 1:
            anomalies.append("SAFE_REDELIVERY")
            commands.append(RepairCommand(
                command="NO_OP_DUPLICATE",
                reason="same event_id and semantic fingerprint",
                evidence_event_ids=[event_id],
            ))
        else:
            anomalies.append("CONFLICTING_EVENT_REUSE")
            commands.extend([
                RepairCommand(command="QUARANTINE_EVENT", reason="same event_id carries conflicting business meaning", evidence_event_ids=[event_id]),
                RepairCommand(command="MANUAL_REVIEW", reason="money-changing repair is blocked until evidence is reviewed", evidence_event_ids=[event_id]),
            ])

    cancellations = [e for e in canonical if e.type == "PICKUP_CANCELLED"]
    if cancellations:
        cancel = cancellations[0]
        stale = [e for e in canonical if e.type in FINANCIAL and e.occurred_at > cancel.occurred_at]
        if stale:
            anomalies.append("LATE_FACT_CAUSALITY")
            if any(e.type == "SETTLED" for e in stale):
                commands.append(RepairCommand(
                    command="REVERSE_SETTLEMENT",
                    reason="settlement occurred after the earlier business-time cancellation",
                    evidence_event_ids=[cancel.event_id] + [e.event_id for e in stale if e.type == "SETTLED"],
                ))
            if any(e.type == "REWARD_GRANTED" for e in stale):
                commands.append(RepairCommand(
                    command="REVERSE_REWARD",
                    reason="reward occurred after the earlier business-time cancellation",
                    evidence_event_ids=[cancel.event_id] + [e.event_id for e in stale if e.type == "REWARD_GRANTED"],
                ))
            commands.append(RepairCommand(
                command="REBUILD_PROJECTION",
                reason="CQRS read model must be rebuilt from canonical event-time order",
                evidence_event_ids=[e.event_id for e in canonical],
            ))

    revisions = [e for e in canonical if e.type == "CAPACITY_REVISED"]
    if revisions and any(e.type == "PICKUP_CONFIRMED" for e in canonical):
        for rev in revisions:
            promised = int(rev.payload.get("promised_units", 0))
            available = int(rev.payload.get("available_units", rev.amount or 0))
            if promised > available:
                anomalies.append("PROMISE_CAPACITY_DRIFT")
                commands.extend([
                    RepairCommand(command="MARK_AT_RISK", reason="confirmed pickup exceeds revised preparation capacity", evidence_event_ids=[rev.event_id]),
                    RepairCommand(command="RESLOT_REVIEW", reason="operator must choose a feasible replacement slot", evidence_event_ids=[rev.event_id]),
                ])
                break

    unique_anomalies = list(dict.fromkeys(anomalies))
    deduped: list[RepairCommand] = []
    seen = set()
    for command in commands:
        key = (command.command, tuple(command.evidence_event_ids))
        if key not in seen:
            seen.add(key)
            deduped.append(command)

    return ReconcileResponse(
        aggregate_id=aggregate_id,
        anomaly_codes=unique_anomalies,
        commands=deduped,
        canonical_event_ids=[e.event_id for e in canonical],
        duplicate_event_ids=sorted(duplicate_ids),
    )
