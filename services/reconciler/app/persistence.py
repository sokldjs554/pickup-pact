from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import UTC, datetime

from .models import EventEnvelope, ReconcileResult


def _stable_digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def archive_raw_events(events: list[EventEnvelope]) -> None:
    """Archive every distinct delivery, not only the first copy of an event ID.

    Conflicting redeliveries intentionally share event_id but differ in payload or
    receipt time. A delivery digest preserves that evidence instead of silently
    overwriting it.
    """
    from pymongo import ASCENDING, MongoClient

    client = MongoClient(os.environ["MONGODB_URL"], serverSelectionTimeoutMS=2000)
    collection = client.pickup_pact.raw_event_deliveries
    collection.create_index("delivery_id", unique=True)
    collection.create_index([("event_id", ASCENDING), ("received_at", ASCENDING)])
    collection.create_index([("aggregate_id", ASCENDING), ("occurred_at", ASCENDING)])

    for event in events:
        document = event.model_dump(mode="json")
        delivery_id = _stable_digest(
            {
                "event_id": document["event_id"],
                "received_at": document["received_at"],
                "event_type": document["event_type"],
                "payload": document["payload"],
            }
        )
        collection.update_one(
            {"delivery_id": delivery_id},
            {"$setOnInsert": {"delivery_id": delivery_id, **document}},
            upsert=True,
        )


def _elasticsearch_client():
    from elasticsearch import Elasticsearch

    return Elasticsearch(
        os.environ["ELASTICSEARCH_URL"],
        request_timeout=2,
        max_retries=3,
        retry_on_timeout=True,
    )


def persistence_readiness() -> dict[str, str]:
    """Check the dependencies required by the persisted reconciliation path."""
    status: dict[str, str] = {}

    try:
        import psycopg

        with psycopg.connect(os.environ["POSTGRES_DSN"], connect_timeout=2) as connection:
            with connection.cursor() as cursor:
                cursor.execute("select 1")
                cursor.fetchone()
        status["postgres"] = "ok"
    except Exception as exc:
        status["postgres"] = f"unavailable:{type(exc).__name__}"

    try:
        from pymongo import MongoClient

        client = MongoClient(os.environ["MONGODB_URL"], serverSelectionTimeoutMS=2000)
        client.admin.command("ping")
        status["mongodb"] = "ok"
    except Exception as exc:
        status["mongodb"] = f"unavailable:{type(exc).__name__}"

    try:
        es = _elasticsearch_client()
        health = es.cluster.health(wait_for_status="yellow", timeout="1s")
        status["elasticsearch"] = (
            "ok" if health.get("timed_out") is False else "unavailable:cluster_timeout"
        )
    except Exception as exc:
        status["elasticsearch"] = f"unavailable:{type(exc).__name__}"

    return status


def index_reconciliation(result: ReconcileResult) -> None:
    document = result.model_dump(mode="json")
    document_id = f"{result.aggregate_id}:{_stable_digest(document)[:24]}"
    es = _elasticsearch_client()
    es.index(
        index="pickup-pact-incidents-v1",
        id=document_id,
        document={**document, "indexed_at": datetime.now(UTC).isoformat()},
    )


def record_reconciliation(result: ReconcileResult) -> str:
    import psycopg

    canonical = result.canonical_state.model_dump(mode="json")
    canonical_hash = _stable_digest(canonical)
    run_fingerprint = _stable_digest(
        {
            "aggregate_id": result.aggregate_id,
            "canonical_hash": canonical_hash,
            "anomalies": result.anomalies,
            "repairs": result.repairs,
            "evidence_event_ids": result.evidence_event_ids,
        }
    )
    run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"pickup-pact:{run_fingerprint}"))

    with psycopg.connect(os.environ["POSTGRES_DSN"]) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                insert into reconciliation_run(
                  run_id, aggregate_id, canonical_hash, anomaly_count, repair_count
                )
                values (%s, %s, %s, %s, %s)
                on conflict (run_id) do nothing
                """,
                (
                    run_id,
                    result.aggregate_id,
                    canonical_hash,
                    len(result.anomalies),
                    len(result.repairs),
                ),
            )
    return run_id



def rebuild_projection(evidence) -> dict:
    """Register supplied facts; never persist an unfenced caller-computed result."""
    from .models import ReconcileRequest
    from .projection_store import capture_evidence, publish_projection
    if isinstance(evidence, ReconcileResult):
        if evidence.decision != "AUTO" or "MANUAL_REVIEW" in evidence.repairs:
            raise ValueError("reconciliation evidence is not executable")
        raise ValueError("server-owned evidence packet is required, not a computed result")
    if not isinstance(evidence, ReconcileRequest):
        raise ValueError("validated evidence packet required")
    return publish_projection(capture_evidence(evidence))


def get_projection(aggregate_id: str) -> dict | None:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(
        os.environ["POSTGRES_DSN"],
        row_factory=dict_row,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                select p.aggregate_id,p.status,p.payment_authorized,p.settled,p.rewarded,
                       p.capacity_revision,p.settlement_post_count,p.reward_post_count,
                       p.canonical_hash,p.rebuilt_at,p.source_revision,p.evidence_hash,
                       h.revision as evidence_revision,
                       (h.revision is not null and p.source_revision=h.revision
                        and p.evidence_hash=h.evidence_hash) as is_current
                from commitment_projection p
                left join projection_evidence_heads h on h.aggregate_id=p.aggregate_id
                where p.aggregate_id = %s
                """,
                (aggregate_id,),
            )
            row = cursor.fetchone()
    return None if row is None else dict(row)
