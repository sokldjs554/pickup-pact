"""Declared expected outcomes. Never imported by either planner."""
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.models import EventEnvelope, ReconcileRequest

BASE = datetime(2026, 9, 23, tzinfo=UTC)


@dataclass(frozen=True)
class Case:
    name: str
    description: str
    request: ReconcileRequest
    decision: str
    repairs: frozenset[str]


def ev(identity, kind, at, *, cause=None, payload=None, receive=None):
    return EventEnvelope(event_id=identity, aggregate_id="audit-order", event_type=kind,
        occurred_at=BASE + timedelta(seconds=at),
        received_at=BASE + timedelta(seconds=at if receive is None else receive),
        causation_id=cause, payload=payload or {})


def suite() -> list[Case]:
    root = [ev("hold", "PickupSlotHeld", 0),
            ev("pay", "PaymentAuthorized", 1, cause="hold"),
            ev("confirm", "CommitmentConfirmed", 2, cause="pay")]
    cancel = ev("cancel", "CommitmentCancelled", 3, cause="confirm", receive=20)
    settle = ev("settle", "SettlementPosted", 4, cause="cancel", payload={"amount": "9000"})
    reward = ev("reward", "RewardGranted", 5, cause="cancel", payload={"amount": "90"})
    def case(name, text, suffix, decision="AUTO", repairs=()):
        return Case(name, text, ReconcileRequest(events=root + suffix), decision, frozenset(repairs))
    return [
        case("normal", "정상 확정 주문", []),
        case("delayed_cancel", "늦게 전달된 확정 취소 뒤 정산·적립", [cancel, settle, reward],
             repairs=("REVERSE_SETTLEMENT", "REVERSE_REWARD")),
        case("exact_duplicate", "정확히 같은 금융 이벤트 재전달", [settle.model_copy(update={"causation_id":"confirm"}),
             settle.model_copy(update={"causation_id":"confirm", "received_at": BASE+timedelta(seconds=30)})]),
        case("conflicting_identity", "동일 정산 ID에 서로 다른 금액", [cancel, settle,
             settle.model_copy(update={"payload":{"amount":"1"}, "received_at":BASE+timedelta(seconds=30)})], "MANUAL_REVIEW"),
        case("terminal_conflict", "취소 확정과 수령 완료가 모두 존재", [cancel,
             ev("claim", "PickupClaimed", 4, cause="confirm"), settle], "MANUAL_REVIEW"),
        case("missing_parent", "정산의 원인 이벤트가 아직 미도착", [cancel,
             settle.model_copy(update={"causation_id":"late"})], "WAIT_FOR_EVIDENCE"),
        case("parent_recovered", "누락 증거 도착 뒤 재평가", [cancel,
             ev("late", "CapacityRevised", 4, cause="cancel", receive=40),
             settle.model_copy(update={"causation_id":"late", "occurred_at":BASE+timedelta(seconds=5)})],
             repairs=("REVERSE_SETTLEMENT",)),
        case("clock_skew", "명시적 원인-결과와 이벤트 시각이 반대", [cancel,
             settle.model_copy(update={"occurred_at":BASE+timedelta(seconds=1), "received_at":BASE+timedelta(seconds=20)})],
             repairs=("REVERSE_SETTLEMENT",)),
        case("causal_cycle", "취소와 정산이 서로를 원인으로 참조", [
             cancel.model_copy(update={"causation_id":"settle"}), settle], "MANUAL_REVIEW"),
        case("timestamp_tie", "같은 시각이며 인과증거 없는 취소·정산", [cancel,
             ev("settle", "SettlementPosted", 3)], "MANUAL_REVIEW"),
        Case("delayed_payment", "결제 증거의 도착만 지연", ReconcileRequest(events=[root[0],
             root[1].model_copy(update={"received_at":BASE+timedelta(seconds=50)}), root[2]]), "AUTO", frozenset()),
        Case("invalid_confirm", "결제 증거 없는 확정 뒤 금융 처리", ReconcileRequest(events=[root[0],
             root[2].model_copy(update={"causation_id":None}), cancel, settle]), "MANUAL_REVIEW", frozenset()),
        case("already_reversed", "이미 역분개된 동일 정산", [cancel, settle,
             ev("reverse", "SettlementReversed", 5, cause="settle")]),
    ]
