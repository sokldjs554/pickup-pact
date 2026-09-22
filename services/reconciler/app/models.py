from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

EventType = Literal[
    "PickupSlotHeld",
    "PaymentAuthorized",
    "CommitmentConfirmed",
    "CommitmentCancelled",
    "SettlementPosted",
    "RewardGranted",
    "CapacityRevised",
    "PickupRescheduled",
    "PickupClaimed",
    "SettlementReversed",
    "RewardReversed",
]

RepairType = Literal[
    "NO_OP_DUPLICATE",
    "REBUILD_PROJECTION",
    "REVERSE_SETTLEMENT",
    "REVERSE_REWARD",
    "RESLOT_REVIEW",
    "MANUAL_REVIEW",
]


class EventEnvelope(BaseModel):
    event_id: str = Field(min_length=1)
    aggregate_id: str = Field(min_length=1)
    event_type: EventType
    occurred_at: datetime
    received_at: datetime
    correlation_id: str | None = None
    causation_id: str | None = None
    schema_version: int = Field(default=1, ge=1)
    payload: dict[str, Any] = Field(default_factory=dict)


class ReconcileRequest(BaseModel):
    events: list[EventEnvelope] = Field(min_length=1)

    @model_validator(mode="after")
    def one_aggregate(self) -> "ReconcileRequest":
        aggregate_ids = {e.aggregate_id for e in self.events}
        if len(aggregate_ids) != 1:
            raise ValueError("all events must belong to one aggregate")
        return self


class Snapshot(BaseModel):
    status: str = "DRAFT"
    payment_authorized: bool = False
    settled: bool = False
    rewarded: bool = False
    capacity_revision: int = 0
    settlement_post_count: int = 0
    reward_post_count: int = 0


class ReconcileResult(BaseModel):
    aggregate_id: str
    receive_order_state: Snapshot
    canonical_state: Snapshot
    anomalies: list[str]
    repairs: list[RepairType]
    duplicate_event_ids: list[str]
    evidence_event_ids: list[str]
