from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import UTC, datetime

from .models import EventEnvelope, ReconcileResult


class StaleProjectionEvidence(ValueError):
    """Incoming rebuild omitted evidence already represented by the projection."""


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



def rebuild_projection(result: ReconcileResult) -> dict:
    """Rebuild only from executable, append-only evidence.

    A later request may add evidence, but it cannot omit event IDs already
    represented by the stored projection. This prevents a stale replay from
    overwriting a newer read model merely because it arrived later.
    """
    if result.decision != "AUTO" or "MANUAL_REVIEW" in result.repairs:
        raise ValueError("reconciliation evidence is not executable")
    import psycopg

    snapshot = result.canonical_state.model_dump(mode="json")
    source_event_ids = sorted(set(result.source_event_ids))
    canonical_hash = _stable_digest(snapshot)
    with psycopg.connect(os.environ["POSTGRES_DSN"]) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                insert into commitment_projection(
                  aggregate_id, status, payment_authorized, settled, rewarded,
                  capacity_revision, settlement_post_count, reward_post_count,
                  canonical_hash, source_event_ids, source_event_count, rebuilt_at
                )
                values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                on conflict (aggregate_id) do update set
                  status = excluded.status,
                  payment_authorized = excluded.payment_authorized,
                  settled = excluded.settled,
                  rewarded = excluded.rewarded,
                  capacity_revision = excluded.capacity_revision,
                  settlement_post_count = excluded.settlement_post_count,
                  reward_post_count = excluded.reward_post_count,
                  canonical_hash = excluded.canonical_hash,
                  source_event_ids = excluded.source_event_ids,
                  source_event_count = excluded.source_event_count,
                  rebuilt_at = now()
                where commitment_projection.source_event_ids <@ excluded.source_event_ids
                """,
                (
                    result.aggregate_id,
                    snapshot["status"],
                    snapshot["payment_authorized"],
                    snapshot["settled"],
                    snapshot["rewarded"],
                    snapshot["capacity_revision"],
                    snapshot["settlement_post_count"],
                    snapshot["reward_post_count"],
                    canonical_hash,
                    source_event_ids,
                    len(source_event_ids),
                ),
            )
            if cursor.rowcount == 0:
                raise StaleProjectionEvidence(
                    "projection rebuild omitted event IDs already represented by the stored projection"
                )
    return {
        **snapshot,
        "canonical_hash": canonical_hash,
        "source_event_ids": source_event_ids,
        "source_event_count": len(source_event_ids),
    }


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
                select aggregate_id, status, payment_authorized, settled, rewarded,
                       capacity_revision, settlement_post_count, reward_post_count,
                       canonical_hash, source_event_ids, source_event_count, rebuilt_at
                from commitment_projection
                where aggregate_id = %s
                """,
                (aggregate_id,),
            )
            row = cursor.fetchone()
    return None if row is None else dict(row)
