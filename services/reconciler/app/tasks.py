import os
from celery import Celery
from .engine import reconcile
from .models import DomainEvent

celery_app = Celery(
    "pickup-pact-reconciler",
    broker=os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0"),
    backend=os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/1"),
)
celery_app.conf.update(task_acks_late=True, task_reject_on_worker_lost=True)

@celery_app.task(
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=5,
)
def replay_aggregate(aggregate_id: str, raw_events: list[dict]) -> dict:
    events = [DomainEvent.model_validate(item) for item in raw_events]
    return reconcile(aggregate_id, events).model_dump(mode="json")
