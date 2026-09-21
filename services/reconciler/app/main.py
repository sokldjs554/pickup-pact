from fastapi import FastAPI, HTTPException
from .engine import reconcile
from .models import ReconcileRequest, ReconcileResponse
from .persistence import EvidenceStore

app = FastAPI(title="Pickup Pact Reconciler", version="0.1.0")
store = EvidenceStore()

@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}

@app.post("/api/v1/reconcile", response_model=ReconcileResponse)
async def reconcile_endpoint(request: ReconcileRequest) -> ReconcileResponse:
    try:
        for event in request.events:
            await store.archive_raw_event(event.model_dump(mode="json"))
        result = reconcile(request.aggregate_id, request.events)
        payload = result.model_dump(mode="json")
        if result.anomaly_codes:
            await store.index_incident(payload)
        await store.append_audit(request.aggregate_id, payload)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
