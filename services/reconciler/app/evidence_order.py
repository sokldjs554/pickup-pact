"""Order *supplied* event evidence without pretending clocks prove causality.

A causation_id used here denotes an antecedent event ID in this aggregate's
provided evidence. Missing references must be supplied by the caller on replay.
No network lookup or automatic cancellation decision is performed here.
"""
from __future__ import annotations

from dataclasses import dataclass
import heapq
from collections.abc import Callable

from .models import EventEnvelope


@dataclass(frozen=True)
class EvidenceOrder:
    events: list[EventEnvelope]
    missing_ids: list[str]
    cyclic_ids: list[str]
    clock_conflict_ids: list[str]
    parents: dict[str, str]

    def precedes(self, ancestor: str, descendant: str) -> bool:
        """Linear-space evidence: do not materialize all transitive ancestors."""
        seen = set()
        cursor = self.parents.get(descendant)
        while cursor is not None and cursor not in seen:
            if cursor == ancestor:
                return True
            seen.add(cursor)
            cursor = self.parents.get(cursor)
        return False


def causal_order(
    events: list[EventEnvelope],
    key: Callable[[EventEnvelope], tuple],
) -> EvidenceOrder:
    """Stable topological ordering; timestamp fallback is for unrelated events.

    IDs are unique on entry. The caller must quarantine conflicting identities
    before treating this order as executable evidence.
    """
    by_id = {event.event_id: event for event in events}
    children: dict[str, list[str]] = {identity: [] for identity in by_id}
    degree = {identity: 0 for identity in by_id}
    missing: set[str] = set()
    skewed: set[str] = set()
    for event in events:
        parent_id = event.causation_id
        if not parent_id:
            continue
        parent = by_id.get(parent_id)
        if parent is None:
            missing.add(parent_id)
            continue
        children[parent_id].append(event.event_id)
        degree[event.event_id] += 1
        if parent.occurred_at > event.occurred_at:
            skewed.update((parent_id, event.event_id))

    ready = [(key(by_id[identity]), identity) for identity, count in degree.items() if count == 0]
    heapq.heapify(ready)
    ordered: list[EventEnvelope] = []
    while ready:
        _, identity = heapq.heappop(ready)
        event = by_id[identity]
        ordered.append(event)
        for child in children[identity]:
            degree[child] -= 1
            if degree[child] == 0:
                heapq.heappush(ready, (key(by_id[child]), child))

    cyclic = sorted(identity for identity, count in degree.items() if count)
    if cyclic:
        # Diagnostic ordering only. The caller blocks all automatic repair.
        ordered = sorted(events, key=key)
    return EvidenceOrder(
        ordered, sorted(missing), cyclic, sorted(skewed),
        {e.event_id: e.causation_id for e in events if e.causation_id in by_id},
    )
