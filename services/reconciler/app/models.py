from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, Field, model_validator

EventType = Literal[
    "PickupSlotHeld",
    "PaymentAuthorized",
    "CommitmentConfirmed",
    "CancellationRequested",
    "CancellationRejected",
    "CommitmentCancelled",
    "SettlementPosted",
    "RewardGranted",
    "CapacityRevised",
    "PickupRescheduled",
    "PickupPactIssued",
    "PickupPactRenegotiated",
    "PickupPactBreached",
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
    occurred_at: AwareDatetime
    received_at: AwareDatetime
    correlation_id: str | None = None
    causation_id: str | None = None
    schema_version: int = Field(default=1, ge=1)
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_capacity_fields(self) -> "EventEnvelope":
        for key in ("capacity_units", "available_units", "revision"):
            if key not in self.payload:
                continue
            value = self.payload[key]
            minimum = 0 if key == "available_units" else 1
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{key} must be an integer >= {minimum}")
        return self


class ReconcileRequest(BaseModel):
    events: list[EventEnvelope] = Field(min_length=1)

    @model_validator(mode="after")
    def one_aggregate(self) -> "ReconcileRequest":
        aggregate_ids = {e.aggregate_id for e in self.events}
        if len(aggregate_ids) != 1:
            raise ValueError("all events must belong to one aggregate")
        return self


class FinancialRepairAction(BaseModel):
    cancellation_event_id: str = Field(min_length=1)
    target_event_id: str = Field(min_length=1)
    repair: Literal["REVERSE_SETTLEMENT", "REVERSE_REWARD"]
    amount: int = Field(gt=0)
    unit: Literal["KRW", "PTS"]


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
    financial_actions: list[FinancialRepairAction] = Field(default_factory=list)
    source_event_ids: list[str] = Field(default_factory=list)
    source_event_fingerprints: dict[str, str] = Field(default_factory=dict)
    decision: Literal["AUTO", "WAIT_FOR_EVIDENCE", "MANUAL_REVIEW"] = "AUTO"
    blocking_reasons: list[str] = Field(default_factory=list)
    missing_event_ids: list[str] = Field(default_factory=list)
