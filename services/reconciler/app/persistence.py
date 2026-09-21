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


def index_reconciliation(result: ReconcileResult) -> None:
    from elasticsearch import Elasticsearch

    document = result.model_dump(mode="json")
    document_id = f"{result.aggregate_id}:{_stable_digest(document)[:24]}"
    es = Elasticsearch(os.environ["ELASTICSEARCH_URL"], request_timeout=2)
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
