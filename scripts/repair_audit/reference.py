"""Small independent conservative reference planner + durable test executor.

This is a conventional reference implementation, NOT a competitor's code.
It does not call app.engine, app.evidence_order, or the scenario oracle.
Both reference and candidate use the same disk-backed idempotent executor.
It records correction INTENTS, never bank refunds or production ledger entries.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3
from typing import Callable

from app.models import ReconcileRequest

FINANCIAL = {"REVERSE_SETTLEMENT", "REVERSE_REWARD"}


@dataclass(frozen=True)
class Outcome:
    decision: str
    repairs: frozenset[str]
    status: str


def semantic(event) -> str:
    return json.dumps(event.model_dump(mode="json", exclude={"received_at"}),
                      sort_keys=True, separators=(",", ":"))


def reference_plan(request: ReconcileRequest) -> Outcome:
    """Deduplicate, validate evidence, topologically replay, then authorize intents.

    Conservative reference, intentionally equipped with the SAME policy guards
    as the candidate, implemented separately. Ties favor manual review. An exact
    retry never changes the economic effect. The reference may be as good as the
    candidate; a tie is reported as a tie, not converted into a marketing win.
    """
    rows = {}
    fingerprints = {}
    for event in request.events:
        fingerprint = semantic(event)
        if event.event_id in fingerprints and fingerprints[event.event_id] != fingerprint:
            return Outcome("MANUAL_REVIEW", frozenset(), "UNRESOLVED")
        rows[event.event_id] = event
        fingerprints[event.event_id] = fingerprint
    types = {e.event_type for e in rows.values()}
    if {"CommitmentCancelled", "PickupClaimed"} <= types:
        return Outcome("MANUAL_REVIEW", frozenset(), "UNRESOLVED")
    if "CommitmentConfirmed" in types and "PaymentAuthorized" not in types:
        return Outcome("MANUAL_REVIEW", frozenset(), "UNRESOLVED")
    if any(e.causation_id and e.causation_id not in rows for e in rows.values()):
        return Outcome("WAIT_FOR_EVIDENCE", frozenset(), "UNRESOLVED")

    # Independent depth-first traversal (candidate uses Kahn's algorithm).
    ordered = []
    visiting = set()
    done = set()
    lineage = {}
    def visit(identity):
        if identity in visiting:
            raise ValueError("cycle")
        if identity in done:
            return
        visiting.add(identity)
        event = rows[identity]
        if event.causation_id:
            visit(event.causation_id)
            lineage[identity] = lineage[event.causation_id] | {event.causation_id}
        else:
            lineage[identity] = set()
        visiting.remove(identity)
        done.add(identity)
        ordered.append(event)
    try:
        for event in sorted(rows.values(), key=lambda e: (e.occurred_at, e.event_id)):
            visit(event.event_id)
    except ValueError:
        return Outcome("MANUAL_REVIEW", frozenset(), "UNRESOLVED")

    authorized = False
    status = "DRAFT"
    open_settlement = open_reward = False
    for event in ordered:
        kind = event.event_type
        if kind == "PaymentAuthorized":
            authorized = True
        elif kind == "CommitmentConfirmed":
            if not authorized:
                return Outcome("MANUAL_REVIEW", frozenset(), "UNRESOLVED")
            status = "CONFIRMED"
        elif kind == "PickupSlotHeld":
            status = "HELD"
        elif kind == "CommitmentCancelled":
            status = "CANCELLED"
        elif kind == "PickupClaimed":
            status = "PICKED_UP"
        elif kind == "SettlementPosted":
            open_settlement = True
        elif kind == "SettlementReversed":
            open_settlement = False
        elif kind == "RewardGranted":
            open_reward = True
        elif kind == "RewardReversed":
            open_reward = False

    cancels = [e for e in rows.values() if e.event_type == "CommitmentCancelled"]
    allowed = set()
    for event_type, is_open, repair in (
        ("SettlementPosted", open_settlement, "REVERSE_SETTLEMENT"),
        ("RewardGranted", open_reward, "REVERSE_REWARD"),
    ):
        for posting in (e for e in rows.values() if e.event_type == event_type):
            for cancel in cancels:
                if cancel.event_id in lineage[posting.event_id]:
                    after = True
                elif posting.event_id in lineage[cancel.event_id]:
                    after = False
                elif cancel.occurred_at == posting.occurred_at:
                    return Outcome("MANUAL_REVIEW", frozenset(), "UNRESOLVED")
                else:
                    after = posting.occurred_at > cancel.occurred_at
                if after and is_open:
                    allowed.add(repair)
    return Outcome("AUTO", frozenset(allowed), status)


def candidate_plan(request: ReconcileRequest) -> Outcome:
    from app.engine import reconcile
    result = reconcile(request)
    return Outcome(result.decision, frozenset(FINANCIAL & set(result.repairs)),
                   result.canonical_state.status)


class DurableExecutor:
    """One full-cancel intent per order/type; not a partial-refund engine.

    Atomic input evidence + correction-intent commit. Preserves conflicting
    payload versions instead of silently accepting the first one. SQLite is a
    local durability probe, NOT a replacement for PostgreSQL/Kafka topology.
    """
    def __init__(self, filename: str | Path):
        self.filename = str(filename)
        with closing(self.connection()) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS inbox (
                    aggregate_id TEXT NOT NULL, event_id TEXT NOT NULL,
                    semantic TEXT NOT NULL, envelope TEXT NOT NULL,
                    PRIMARY KEY (aggregate_id,event_id,semantic)
                );
                CREATE TABLE IF NOT EXISTS effects (
                    aggregate_id TEXT NOT NULL, repair TEXT NOT NULL,
                    PRIMARY KEY (aggregate_id,repair)
                );
            ''')

    def connection(self):
        db = sqlite3.connect(self.filename, timeout=10)
        db.execute("PRAGMA synchronous=FULL")
        return db

    def apply(self, request: ReconcileRequest, planner: Callable, crash: str = "none") -> Outcome:
        db = self.connection()
        try:
            db.execute("BEGIN IMMEDIATE")
            aggregate_id = request.events[0].aggregate_id
            for event in request.events:
                db.execute("INSERT OR IGNORE INTO inbox VALUES(?,?,?,?)", (
                    aggregate_id, event.event_id, semantic(event), event.model_dump_json()))
            if crash == "after_inbox":
                os._exit(77)
            raw = [json.loads(r[0]) for r in db.execute(
                "SELECT envelope FROM inbox WHERE aggregate_id=? ORDER BY event_id,semantic", (aggregate_id,))]
            result = planner(ReconcileRequest(events=raw))
            if result.decision == "AUTO":
                for repair in result.repairs:
                    db.execute("INSERT OR IGNORE INTO effects VALUES(?,?)", (aggregate_id, repair))
            if crash == "before_commit":
                os._exit(77)
            db.commit()
            if crash == "after_commit":
                os._exit(77)
            return result
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def effects(self):
        with closing(self.connection()) as db:
            return list(db.execute("SELECT aggregate_id,repair FROM effects ORDER BY 1,2"))
