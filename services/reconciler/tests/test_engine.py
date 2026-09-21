from datetime import datetime, timedelta, timezone
from services.reconciler.app.engine import reconcile
from services.reconciler.app.models import DomainEvent

UTC=timezone.utc
BASE=datetime(2026,9,21,12,0,tzinfo=UTC)

def event(event_id, kind, occurred, received, amount=None, payload=None):
    return DomainEvent(
        event_id=event_id, aggregate_id="o-1", type=kind,
        occurred_at=BASE+timedelta(seconds=occurred),
        received_at=BASE+timedelta(seconds=received),
        amount=amount, payload=payload or {},
    )

def test_late_cancellation_produces_compensating_commands():
    result=reconcile("o-1",[
        event("confirm","PICKUP_CONFIRMED",1,1),
        event("cancel","PICKUP_CANCELLED",10,60),
        event("settle","SETTLED",20,20,9000),
        event("reward","REWARD_GRANTED",21,21,90),
    ])
    commands={x.command for x in result.commands}
    assert {"REVERSE_SETTLEMENT","REVERSE_REWARD","REBUILD_PROJECTION"} <= commands
    assert "LATE_FACT_CAUSALITY" in result.anomaly_codes

def test_identical_redelivery_is_noop():
    e=event("same","SETTLED",10,10,5500)
    e2=event("same","SETTLED",10,30,5500)
    result=reconcile("o-1",[e,e2])
    assert "SAFE_REDELIVERY" in result.anomaly_codes
    assert any(x.command=="NO_OP_DUPLICATE" for x in result.commands)

def test_conflicting_id_is_quarantined():
    result=reconcile("o-1",[
        event("same","SETTLED",10,10,11500),
        event("same","SETTLED",10,20,13500),
    ])
    assert "CONFLICTING_EVENT_REUSE" in result.anomaly_codes
    assert any(x.command=="MANUAL_REVIEW" for x in result.commands)

def test_capacity_revision_marks_at_risk():
    result=reconcile("o-1",[
        event("c","PICKUP_CONFIRMED",1,1),
        event("r","CAPACITY_REVISED",2,2,payload={"promised_units":4,"available_units":2}),
    ])
    assert any(x.command=="MARK_AT_RISK" for x in result.commands)
