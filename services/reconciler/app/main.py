from __future__ import annotations

import os

if os.getenv("DD_TRACE_ENABLED", "false").lower() == "true":
    from ddtrace import patch_all

    patch_all()

from fastapi import FastAPI, HTTPException

from .ai_review import provider_from_env
from .engine import reconcile
from .models import ReconcileRequest, ReconcileResult

app = FastAPI(
    title="Pickup Pact Reconciler",
    version="0.1.0",
    description="Canonical event-time replay and deterministic repair planning.",
)

if os.getenv("ELASTIC_APM_ENABLED", "false").lower() == "true":
    from elasticapm.contrib.starlette import ElasticAPM, make_apm_client

    apm_client = make_apm_client(
        {
            "SERVICE_NAME": os.getenv("ELASTIC_APM_SERVICE_NAME", "pickup-pact-reconciler"),
            "SERVER_URL": os.environ["ELASTIC_APM_SERVER_URL"],
            "ENVIRONMENT": os.getenv("ENVIRONMENT", "local"),
        }
    )
    app.add_middleware(ElasticAPM, client=apm_client)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness only: the process can serve requests."""
    return {"status": "ok"}


@app.get("/ready")
def ready() -> dict:
    """Readiness includes external stores used by persisted reconciliation."""
    if os.getenv("PERSIST_RECONCILIATION", "false").lower() != "true":
        return {"status": "ready", "mode": "stateless", "dependencies": {}}

    from .persistence import persistence_readiness

    dependencies = persistence_readiness()
    if any(value != "ok" for value in dependencies.values()):
        raise HTTPException(
            status_code=503,
            detail={"status": "not_ready", "dependencies": dependencies},
        )
    return {
        "status": "ready",
        "mode": "persisted",
        "dependencies": dependencies,
    }


@app.post("/api/v1/reconcile", response_model=ReconcileResult)
def reconcile_api(request: ReconcileRequest) -> ReconcileResult:
    result = reconcile(request)
    if os.getenv("PERSIST_RECONCILIATION", "false").lower() == "true":
        from .persistence import archive_raw_events, index_reconciliation, record_reconciliation

        archive_raw_events(request.events)
        index_reconciliation(result)
        record_reconciliation(result)
    return result


@app.post("/api/v1/replay")
def replay_api(request: ReconcileRequest) -> dict:
    result = reconcile(request)
    review = provider_from_env().review(result)
    return {
        "reconciliation": result.model_dump(mode="json"),
        "ai_review": {
            "provider": review.provider,
            "summary": review.summary,
            "hypotheses": list(review.hypotheses),
            "evidence_event_ids": list(review.evidence_event_ids),
            "advisory_only": True,
        },
    }


@app.get("/api/v1/incidents/{aggregate_id}")
def incidents_api(aggregate_id: str) -> dict:
    from elasticsearch import Elasticsearch, NotFoundError

    es = Elasticsearch(
        os.environ.get("ELASTICSEARCH_URL", "http://localhost:9200"),
        request_timeout=2,
        max_retries=3,
        retry_on_timeout=True,
    )
    try:
        response = es.search(
            index="pickup-pact-incidents-v1",
            query={"term": {"aggregate_id.keyword": aggregate_id}},
            sort=[{"indexed_at": {"order": "desc"}}],
            size=50,
        )
    except NotFoundError:
        return {"aggregate_id": aggregate_id, "incidents": []}
    return {
        "aggregate_id": aggregate_id,
        "incidents": [hit["_source"] for hit in response["hits"]["hits"]],
    }


@app.post("/api/v1/projections/rebuild")
def rebuild_projection_api(request: ReconcileRequest) -> dict:
    from .persistence import StaleProjectionEvidence, rebuild_projection

    result = reconcile(request)
    if result.decision != "AUTO" or "MANUAL_REVIEW" in result.repairs:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "unresolved_reconciliation_evidence",
                "decision": result.decision,
                "blocking_reasons": result.blocking_reasons,
                "missing_event_ids": result.missing_event_ids,
            },
        )
    try:
        projection = rebuild_projection(result)
    except StaleProjectionEvidence as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "stale_projection_evidence",
                "message": str(exc),
                "source_event_ids": result.source_event_ids,
            },
        ) from exc
    return {
        "aggregate_id": result.aggregate_id,
        "source": "canonical-event-time",
        "projection": projection,
    }


@app.get("/api/v1/projections/{aggregate_id}")
def projection_api(aggregate_id: str) -> dict:
    from .persistence import get_projection

    projection = get_projection(aggregate_id)
    if projection is None:
        raise HTTPException(status_code=404, detail="projection not found")
    return {
        "aggregate_id": aggregate_id,
        "projection": projection,
    }
