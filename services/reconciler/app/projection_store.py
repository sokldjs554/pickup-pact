"""Server-owned evidence revisions and compare-and-swap projection publication.

This fence covers facts registered in this database, not undelivered source facts.
No caller-provided revision is accepted as evidence authority.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
import os

from .engine import reconcile
from .models import EventEnvelope, ReconcileRequest, ReconcileResult


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def semantic(event: EventEnvelope) -> dict:
    return event.model_dump(mode='json', exclude={'received_at'})


def evidence_digest(events) -> str:
    return digest(sorted((semantic(e) for e in events),
                         key=lambda e: json.dumps(e, sort_keys=True)))


@dataclass(frozen=True)
class ProjectionCandidate:
    aggregate_id: str
    revision: int
    evidence_hash: str
    events: tuple[EventEnvelope, ...]
    result: ReconcileResult


class StaleProjection(ValueError):
    pass


class UnresolvedProjection(ValueError):
    def __init__(self, result: ReconcileResult):
        super().__init__('reconciliation evidence is not executable')
        self.result = result


def capture_evidence(request: ReconcileRequest) -> ProjectionCandidate:
    import psycopg
    aggregate_id = request.events[0].aggregate_id
    with psycopg.connect(os.environ['POSTGRES_DSN']) as db:
        with db.cursor() as cursor:
            cursor.execute('''insert into projection_evidence_heads(aggregate_id,revision,evidence_hash)
                              values(%s,0,%s) on conflict(aggregate_id) do nothing''',
                           (aggregate_id, digest([])))
            cursor.execute('select revision from projection_evidence_heads where aggregate_id=%s for update',
                           (aggregate_id,))
            revision = cursor.fetchone()[0]
            changed = False
            for event in request.events:
                cursor.execute('''insert into projection_evidence_events(aggregate_id,event_id,semantic_hash,envelope)
                                  values(%s,%s,%s,%s::jsonb) on conflict do nothing''',
                               (aggregate_id,event.event_id,digest(semantic(event)),event.model_dump_json()))
                changed = changed or cursor.rowcount > 0
            cursor.execute('''select envelope from projection_evidence_events where aggregate_id=%s
                              order by event_id,semantic_hash''', (aggregate_id,))
            events = tuple(EventEnvelope.model_validate(row[0]) for row in cursor.fetchall())
            hashed = evidence_digest(events)
            if changed:
                revision += 1
                cursor.execute('''update projection_evidence_heads set revision=%s,evidence_hash=%s
                                  where aggregate_id=%s''', (revision,hashed,aggregate_id))
    return ProjectionCandidate(aggregate_id,revision,hashed,events,reconcile(ReconcileRequest(events=list(events))))


def validate_candidate(candidate: ProjectionCandidate) -> dict:
    if not isinstance(candidate, ProjectionCandidate):
        raise ValueError('server-owned evidence candidate required')
    if candidate.result.decision != 'AUTO' or 'MANUAL_REVIEW' in candidate.result.repairs:
        raise UnresolvedProjection(candidate.result)
    if candidate.revision < 1 or not candidate.events:
        raise ValueError('invalid evidence revision')
    if any(e.aggregate_id != candidate.aggregate_id for e in candidate.events):
        raise ValueError('mixed aggregate evidence')
    if evidence_digest(candidate.events) != candidate.evidence_hash:
        raise ValueError('evidence digest mismatch')
    # The candidate is an internal object, not a capability permitting arbitrary state.
    replay = reconcile(ReconcileRequest(events=list(candidate.events)))
    if replay != candidate.result:
        raise ValueError('candidate result does not match its evidence')
    return replay.canonical_state.model_dump(mode='json')


def publish_projection(candidate: ProjectionCandidate) -> dict:
    snapshot = validate_candidate(candidate)
    canonical_hash = digest(snapshot)
    import psycopg
    with psycopg.connect(os.environ['POSTGRES_DSN']) as db:
        with db.cursor() as cursor:
            cursor.execute('''select revision,evidence_hash from projection_evidence_heads
                              where aggregate_id=%s for update''', (candidate.aggregate_id,))
            head = cursor.fetchone()
            if head is None or (head[0], head[1]) != (candidate.revision, candidate.evidence_hash):
                raise StaleProjection('new evidence arrived; rebuild from the registered head')
            cursor.execute('''
                insert into commitment_projection(
                  aggregate_id,status,payment_authorized,settled,rewarded,capacity_revision,
                  settlement_post_count,reward_post_count,canonical_hash,source_revision,evidence_hash,rebuilt_at)
                values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                on conflict(aggregate_id) do update set
                  status=excluded.status,payment_authorized=excluded.payment_authorized,
                  settled=excluded.settled,rewarded=excluded.rewarded,
                  capacity_revision=excluded.capacity_revision,
                  settlement_post_count=excluded.settlement_post_count,reward_post_count=excluded.reward_post_count,
                  canonical_hash=excluded.canonical_hash,source_revision=excluded.source_revision,
                  evidence_hash=excluded.evidence_hash,rebuilt_at=now()
                where commitment_projection.source_revision < excluded.source_revision
                ''', (candidate.aggregate_id,snapshot['status'],snapshot['payment_authorized'],
                      snapshot['settled'],snapshot['rewarded'],snapshot['capacity_revision'],
                      snapshot['settlement_post_count'],snapshot['reward_post_count'],canonical_hash,
                      candidate.revision,candidate.evidence_hash))
            if cursor.rowcount == 0:
                cursor.execute('''select source_revision,evidence_hash,canonical_hash from commitment_projection
                                  where aggregate_id=%s''', (candidate.aggregate_id,))
                current = cursor.fetchone()
                if current != (candidate.revision,candidate.evidence_hash,canonical_hash):
                    raise StaleProjection('projection version or content conflicts with this candidate')
    return {**snapshot,'canonical_hash':canonical_hash,'source_revision':candidate.revision,
            'evidence_hash':candidate.evidence_hash,'is_current':True}
