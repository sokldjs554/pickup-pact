from __future__ import annotations
from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, Field

EventType = Literal[
    "PICKUP_CONFIRMED", "PICKUP_CANCELLED", "PAYMENT_AUTHORIZED",
    "SETTLED", "REWARD_GRANTED", "CAPACITY_REVISED"
]

class DomainEvent(BaseModel):
    event_id: str
    aggregate_id: str
    type: EventType
    occurred_at: datetime
    received_at: datetime
    amount: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

class RepairCommand(BaseModel):
    command: Literal[
        "REVERSE_SETTLEMENT", "REVERSE_REWARD", "REBUILD_PROJECTION",
        "NO_OP_DUPLICATE", "VERIFY_PROJECTION", "MARK_AT_RISK",
        "RESLOT_REVIEW", "QUARANTINE_EVENT", "MANUAL_REVIEW"
    ]
    reason: str
    evidence_event_ids: list[str]

class ReconcileRequest(BaseModel):
    aggregate_id: str
    events: list[DomainEvent]

class ReconcileResponse(BaseModel):
    aggregate_id: str
    anomaly_codes: list[str]
    commands: list[RepairCommand]
    canonical_event_ids: list[str]
    duplicate_event_ids: list[str]
