from __future__ import annotations

import hashlib,json,os,uuid
from datetime import UTC,datetime
from .models import EventEnvelope,ReconcileResult

def _stable_digest(payload: object)->str:
    return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(",",":"),default=str).encode()).hexdigest()

def archive_raw_events(events:list[EventEnvelope])->None:
    from pymongo import MongoClient
    client=MongoClient(os.environ["MONGODB_URL"],serverSelectionTimeoutMS=2000); collection=client.pickup_pact.raw_events; collection.create_index("event_id",unique=True)
    for event in events: collection.update_one({"event_id":event.event_id},{"$setOnInsert":event.model_dump(mode="json")},upsert=True)

def index_reconciliation(result:ReconcileResult)->None:
    from elasticsearch import Elasticsearch
    document=result.model_dump(mode="json"); document_id=f"{result.aggregate_id}:{_stable_digest(document)[:24]}"; es=Elasticsearch(os.environ["ELASTICSEARCH_URL"],request_timeout=2); es.index(index="pickup-pact-incidents-v1",id=document_id,document={**document,"indexed_at":datetime.now(UTC).isoformat()})

def record_reconciliation(result:ReconcileResult)->str:
    import psycopg
    canonical=result.canonical_state.model_dump(mode="json"); canonical_hash=_stable_digest(canonical); run_fingerprint=_stable_digest({"aggregate_id":result.aggregate_id,"canonical_hash":canonical_hash,"anomalies":result.anomalies,"repairs":result.repairs,"evidence_event_ids":result.evidence_event_ids}); run_id=str(uuid.uuid5(uuid.NAMESPACE_URL,f"pickup-pact:{run_fingerprint}"))
    with psycopg.connect(os.environ["POSTGRES_DSN"]) as connection:
        with connection.cursor() as cursor: cursor.execute("INSERT INTO reconciliation_run(run_id, aggregate_id, canonical_hash, anomaly_count, repair_count) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (run_id) DO NOTHING",(run_id,result.aggregate_id,canonical_hash,len(result.anomalies),len(result.repairs)))
    return run_id
