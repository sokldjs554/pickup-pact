from __future__ import annotations
import os
from celery import Celery
from .engine import reconcile
from .models import ReconcileRequest
celery_app=Celery("pickup_pact",broker=os.getenv("REDIS_URL","redis://localhost:6379/0"),backend=os.getenv("REDIS_URL","redis://localhost:6379/0")); celery_app.conf.task_routes={"app.tasks.reconcile_packet":{"queue":"reconciliation"}}
@celery_app.task(name="app.tasks.reconcile_packet",autoretry_for=(Exception,),retry_backoff=True,max_retries=3)
def reconcile_packet(payload:dict)->dict:
    request=ReconcileRequest.model_validate(payload); result=reconcile(request)
    if os.getenv("PERSIST_RECONCILIATION","false").lower()=="true":
        from .persistence import archive_raw_events,index_reconciliation,record_reconciliation
        archive_raw_events(request.events); index_reconciliation(result); record_reconciliation(result)
    return result.model_dump(mode="json")
