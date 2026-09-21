from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from services.reconciler.app.engine import reconcile as core_reconcile
from services.reconciler.app.models import EventEnvelope, ReconcileRequest
from demo.sandbox import demo_store

app = FastAPI(
    title="Pickup Pact Demo",
    version="2.0.0",
    description="Interviewer-facing smart-order consistency demo backed by the real reconciliation engine.",
)

INDEX = Path(__file__).with_name("index.html")

EVENT_LABELS = {
    "PickupSlotHeld": "픽업 슬롯 확보",
    "PaymentAuthorized": "결제 승인",
    "CommitmentConfirmed": "픽업 주문 확정",
    "CommitmentCancelled": "고객 주문 취소",
    "SettlementPosted": "점주 정산 반영",
    "RewardGranted": "고객 포인트 적립",
    "CapacityRevised": "매장 처리량 변경",
    "SettlementReversed": "정산 취소 분개",
    "RewardReversed": "포인트 회수",
}

ANOMALY_COPY = {
    "duplicate_event_delivery": (
        "같은 이벤트가 두 번 도착했습니다.",
        "Kafka의 at-least-once 전달 때문에 동일한 금전 이벤트가 재전달되었습니다.",
    ),
    "conflicting_duplicate_payload": (
        "같은 이벤트 ID인데 금액이 다릅니다.",
        "안전한 재전달로 볼 수 없어 자동 반영을 중단하고 사람 검토가 필요합니다.",
    ),
    "receive_order_projection_drift": (
        "서버가 받은 순서와 실제 발생 순서가 다릅니다.",
        "수신 순서만 믿으면 주문·정산·적립 상태가 실제 업무 순서와 어긋납니다.",
    ),
    "confirmed_without_payment_authorization": (
        "결제 승인 없이 픽업이 확정되었습니다.",
        "픽업 약속을 확정하기 위한 선행 조건이 충족되지 않았습니다.",
    ),
    "confirmed_promise_exceeds_revised_capacity": (
        "이미 약속한 픽업 수량을 매장이 처리할 수 없습니다.",
        "확정 뒤 매장 처리 가능 수량이 줄어 기존 픽업 약속이 위험 상태가 되었습니다.",
    ),
    "settlement_posted_after_prior_cancellation": (
        "취소 뒤에 점주 정산이 반영되었습니다.",
        "고객 취소가 실제로 먼저 발생했기 때문에 정산을 그대로 둘 수 없습니다.",
    ),
    "reward_granted_after_prior_cancellation": (
        "취소 뒤에 포인트가 지급되었습니다.",
        "취소된 주문에 지급된 포인트를 회수해야 합니다.",
    ),
}

REPAIR_COPY = {
    "NO_OP_DUPLICATE": (
        "두 번째 금전 반영 차단",
        "같은 이벤트를 다시 받아도 정산·적립을 한 번 더 만들지 않습니다.",
    ),
    "REBUILD_PROJECTION": (
        "주문 조회 상태 재구성",
        "실제 발생 순서를 기준으로 고객/점주 화면의 상태를 다시 계산합니다.",
    ),
    "REVERSE_SETTLEMENT": (
        "잘못된 점주 정산 취소",
        "기존 정산 기록은 삭제하지 않고 반대 분개를 새로 만들어 감사 이력을 유지합니다.",
    ),
    "REVERSE_REWARD": (
        "잘못 지급된 포인트 회수",
        "취소 이후 지급된 포인트를 보상 이벤트로 회수합니다.",
    ),
    "RESLOT_REVIEW": (
        "대체 픽업 시간 검토",
        "현재 처리량으로 지킬 수 있는 가장 가까운 슬롯을 다시 검토합니다.",
    ),
    "MANUAL_REVIEW": (
        "자동 금전 변경 중단",
        "이벤트 의미가 충돌하므로 자동 보정 대신 근거를 묶어 운영자 검토로 보냅니다.",
    ),
}

SCENARIOS: dict[str, dict[str, Any]] = {
    "late-cancel": {
        "title": "취소 메시지가 늦게 도착한 주문",
        "tab_title": "취소 메시지 지연",
        "tab_subtitle": "취소는 먼저, 서버 도착은 나중",
        "customer": "12:30 픽업 · 아메리카노 2잔 · 9,000원",
        "problem": "고객은 12:14:10에 취소했지만 취소 메시지는 52초 뒤에 도착했습니다. 그 사이 점주 정산과 포인트 적립이 먼저 처리되었습니다.",
        "order": {
            "order_id": "PP-1208",
            "store": "패스카페 강남역점",
            "items": "아메리카노 2잔",
            "total": "9,000원",
            "pickup_at": "12:30",
            "customer_action": "12:14:10 주문 취소",
        },
        "business_impact": {
            "customer": "취소한 주문의 포인트가 남아 잘못 사용할 수 있음",
            "merchant": "취소 주문 대금 9,000원이 정산 대상으로 남음",
            "service": "주문·정산·적립 상태가 서로 다른 사실을 가리킴",
        },
        "before": "주문 취소 / 정산 유지 / 90P 유지",
        "after": "주문 취소 / 정산 취소 / 90P 회수",
        "events": [
            {
                "event_id": "slot-101",
                "aggregate_id": "order-late-cancel",
                "event_type": "PickupSlotHeld",
                "occurred_at": "2026-09-21T12:08:00+09:00",
                "received_at": "2026-09-21T12:08:01+09:00",
                "payload": {"capacity_units": 2, "pickup_at": "2026-09-21T12:30:00+09:00"},
                "detail": "12:30 픽업 슬롯 2개 확보",
            },
            {
                "event_id": "payment-102",
                "aggregate_id": "order-late-cancel",
                "event_type": "PaymentAuthorized",
                "occurred_at": "2026-09-21T12:08:02+09:00",
                "received_at": "2026-09-21T12:08:03+09:00",
                "payload": {"amount": "9000"},
                "detail": "결제 9,000원 승인",
            },
            {
                "event_id": "confirm-103",
                "aggregate_id": "order-late-cancel",
                "event_type": "CommitmentConfirmed",
                "occurred_at": "2026-09-21T12:08:04+09:00",
                "received_at": "2026-09-21T12:08:05+09:00",
                "payload": {"capacity_units": 2, "pickup_at": "2026-09-21T12:30:00+09:00"},
                "detail": "고객에게 12:30 픽업 확정",
            },
            {
                "event_id": "cancel-104",
                "aggregate_id": "order-late-cancel",
                "event_type": "CommitmentCancelled",
                "occurred_at": "2026-09-21T12:14:10+09:00",
                "received_at": "2026-09-21T12:15:02+09:00",
                "payload": {"reason": "customer_request"},
                "detail": "고객 취소 — 실제 발생 후 52초 늦게 서버 도착",
            },
            {
                "event_id": "settlement-105",
                "aggregate_id": "order-late-cancel",
                "event_type": "SettlementPosted",
                "occurred_at": "2026-09-21T12:14:36+09:00",
                "received_at": "2026-09-21T12:14:36+09:00",
                "payload": {"amount": "9000"},
                "detail": "점주 정산 9,000원 반영",
            },
            {
                "event_id": "reward-106",
                "aggregate_id": "order-late-cancel",
                "event_type": "RewardGranted",
                "occurred_at": "2026-09-21T12:14:40+09:00",
                "received_at": "2026-09-21T12:14:40+09:00",
                "payload": {"amount": "90"},
                "detail": "고객 포인트 90P 적립",
            },
        ],
    },
    "duplicate": {
        "title": "같은 정산 이벤트가 두 번 도착한 주문",
        "tab_title": "같은 정산 2번",
        "tab_subtitle": "Kafka 중복 전달",
        "customer": "13:00 픽업 · 카페라떼 1잔 · 5,500원",
        "problem": "동일한 정산 이벤트가 재전달되었습니다. 중복을 막지 못하면 점주 정산이 두 번 반영될 수 있습니다.",
        "order": {
            "order_id": "PP-1300",
            "store": "패스카페 역삼점",
            "items": "카페라떼 1잔",
            "total": "5,500원",
            "pickup_at": "13:00",
            "customer_action": "정상 픽업",
        },
        "business_impact": {
            "customer": "고객 화면은 정상이어도 내부 금전 상태가 틀어질 수 있음",
            "merchant": "동일 주문 정산이 두 번 잡힐 위험",
            "service": "at-least-once 메시징의 중복 부작용 가능",
        },
        "before": "정산 이벤트 2회 수신",
        "after": "금전 반영 1회만 유지",
        "events": [
            {
                "event_id": "slot-201",
                "aggregate_id": "order-duplicate",
                "event_type": "PickupSlotHeld",
                "occurred_at": "2026-09-21T12:44:00+09:00",
                "received_at": "2026-09-21T12:44:00+09:00",
                "payload": {"capacity_units": 1},
                "detail": "13:00 픽업 슬롯 확보",
            },
            {
                "event_id": "pay-202",
                "aggregate_id": "order-duplicate",
                "event_type": "PaymentAuthorized",
                "occurred_at": "2026-09-21T12:44:01+09:00",
                "received_at": "2026-09-21T12:44:01+09:00",
                "payload": {"amount": "5500"},
                "detail": "결제 5,500원 승인",
            },
            {
                "event_id": "confirm-203",
                "aggregate_id": "order-duplicate",
                "event_type": "CommitmentConfirmed",
                "occurred_at": "2026-09-21T12:44:02+09:00",
                "received_at": "2026-09-21T12:44:02+09:00",
                "payload": {"capacity_units": 1},
                "detail": "13:00 픽업 확정",
            },
            {
                "event_id": "settlement-77",
                "aggregate_id": "order-duplicate",
                "event_type": "SettlementPosted",
                "occurred_at": "2026-09-21T12:44:05+09:00",
                "received_at": "2026-09-21T12:44:06+09:00",
                "payload": {"amount": "5500"},
                "detail": "정산 5,500원 반영",
            },
            {
                "event_id": "settlement-77",
                "aggregate_id": "order-duplicate",
                "event_type": "SettlementPosted",
                "occurred_at": "2026-09-21T12:44:05+09:00",
                "received_at": "2026-09-21T12:44:19+09:00",
                "payload": {"amount": "5500"},
                "detail": "동일 Kafka 이벤트 재전달",
            },
        ],
    },
    "capacity-drop": {
        "title": "확정 뒤 매장 처리량이 줄어든 주문",
        "tab_title": "매장 처리량 감소",
        "tab_subtitle": "확정한 픽업 약속 위험",
        "customer": "18:10 픽업 · 음료 4잔 · 22,000원",
        "problem": "4잔을 만들 수 있다고 보고 주문을 확정했지만 머신 장애로 해당 시간대 처리 가능 수량이 2잔으로 줄었습니다.",
        "order": {
            "order_id": "PP-1810",
            "store": "패스카페 성수점",
            "items": "음료 4잔",
            "total": "22,000원",
            "pickup_at": "18:10",
            "customer_action": "픽업 대기",
        },
        "business_impact": {
            "customer": "약속한 시간에 음료를 받지 못할 가능성",
            "merchant": "현장에서 주문 지연·문의가 집중될 수 있음",
            "service": "확정 당시 상태와 현재 제조 가능량이 불일치",
        },
        "before": "18:10 픽업 확정",
        "after": "AT_RISK / 대체 시간 검토",
        "events": [
            {
                "event_id": "slot-301",
                "aggregate_id": "order-capacity",
                "event_type": "PickupSlotHeld",
                "occurred_at": "2026-09-21T17:48:00+09:00",
                "received_at": "2026-09-21T17:48:00+09:00",
                "payload": {"capacity_units": 4},
                "detail": "18:10 제조 슬롯 4잔 확보",
            },
            {
                "event_id": "pay-302",
                "aggregate_id": "order-capacity",
                "event_type": "PaymentAuthorized",
                "occurred_at": "2026-09-21T17:48:01+09:00",
                "received_at": "2026-09-21T17:48:01+09:00",
                "payload": {"amount": "22000"},
                "detail": "결제 22,000원 승인",
            },
            {
                "event_id": "confirm-303",
                "aggregate_id": "order-capacity",
                "event_type": "CommitmentConfirmed",
                "occurred_at": "2026-09-21T17:48:03+09:00",
                "received_at": "2026-09-21T17:48:04+09:00",
                "payload": {"capacity_units": 4},
                "detail": "18:10 픽업 4잔 확정",
            },
            {
                "event_id": "capacity-304",
                "aggregate_id": "order-capacity",
                "event_type": "CapacityRevised",
                "occurred_at": "2026-09-21T17:55:00+09:00",
                "received_at": "2026-09-21T17:55:01+09:00",
                "payload": {"revision": 2, "available_units": 2},
                "detail": "머신 장애로 처리 가능량 4 → 2",
            },
        ],
    },
    "conflict": {
        "title": "같은 이벤트 ID인데 금액이 다른 주문",
        "tab_title": "같은 ID, 다른 금액",
        "tab_subtitle": "단순 중복이 아닌 충돌",
        "customer": "09:20 픽업 · 샌드위치 세트 · 11,500원",
        "problem": "동일 event_id가 두 번 도착했지만 한 번은 11,500원, 다른 한 번은 13,500원입니다. 자동 dedupe하면 상류 오류를 숨길 수 있습니다.",
        "order": {
            "order_id": "PP-0920",
            "store": "패스카페 시청점",
            "items": "샌드위치 세트",
            "total": "11,500원",
            "pickup_at": "09:20",
            "customer_action": "정상 픽업",
        },
        "business_impact": {
            "customer": "잘못된 금액 처리 가능성",
            "merchant": "정산 금액 신뢰성 훼손",
            "service": "동일 ID의 의미 충돌을 조용히 버리면 장애 근거가 사라짐",
        },
        "before": "11,500원 / 13,500원 충돌",
        "after": "자동 반영 중단 / 운영자 검토",
        "events": [
            {
                "event_id": "slot-401",
                "aggregate_id": "order-conflict",
                "event_type": "PickupSlotHeld",
                "occurred_at": "2026-09-21T09:00:50+09:00",
                "received_at": "2026-09-21T09:00:50+09:00",
                "payload": {"capacity_units": 1},
                "detail": "09:20 픽업 슬롯 확보",
            },
            {
                "event_id": "pay-402",
                "aggregate_id": "order-conflict",
                "event_type": "PaymentAuthorized",
                "occurred_at": "2026-09-21T09:00:52+09:00",
                "received_at": "2026-09-21T09:00:52+09:00",
                "payload": {"amount": "11500"},
                "detail": "결제 11,500원 승인",
            },
            {
                "event_id": "confirm-403",
                "aggregate_id": "order-conflict",
                "event_type": "CommitmentConfirmed",
                "occurred_at": "2026-09-21T09:00:55+09:00",
                "received_at": "2026-09-21T09:00:55+09:00",
                "payload": {"capacity_units": 1},
                "detail": "09:20 픽업 확정",
            },
            {
                "event_id": "settlement-X",
                "aggregate_id": "order-conflict",
                "event_type": "SettlementPosted",
                "occurred_at": "2026-09-21T09:01:00+09:00",
                "received_at": "2026-09-21T09:01:01+09:00",
                "payload": {"amount": "11500"},
                "detail": "정산 11,500원",
            },
            {
                "event_id": "settlement-X",
                "aggregate_id": "order-conflict",
                "event_type": "SettlementPosted",
                "occurred_at": "2026-09-21T09:01:00+09:00",
                "received_at": "2026-09-21T09:01:18+09:00",
                "payload": {"amount": "13500"},
                "detail": "동일 ID지만 정산 금액 13,500원",
            },
        ],
    },
}


def _event_time(value: datetime) -> str:
    return value.astimezone().strftime("%H:%M:%S")


def _build_request(scenario: dict[str, Any]) -> ReconcileRequest:
    events = [
        EventEnvelope(
            event_id=item["event_id"],
            aggregate_id=item["aggregate_id"],
            event_type=item["event_type"],
            occurred_at=item["occurred_at"],
            received_at=item["received_at"],
            payload=item["payload"],
        )
        for item in scenario["events"]
    ]
    return ReconcileRequest(events=events)


def _ui_event(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": item["event_id"],
        "event_type": item["event_type"],
        "label": EVENT_LABELS[item["event_type"]],
        "occurred_at": datetime.fromisoformat(item["occurred_at"]).strftime("%H:%M:%S"),
        "received_at": datetime.fromisoformat(item["received_at"]).strftime("%H:%M:%S"),
        "detail": item["detail"],
    }


def _snapshot_label(result: Any) -> str:
    state = result.canonical_state
    parts = [state.status]
    if state.settled:
        parts.append("정산 반영")
    if state.rewarded:
        parts.append("포인트 반영")
    return " / ".join(parts)


def run_scenario(scenario_id: str) -> dict[str, Any]:
    if scenario_id not in SCENARIOS:
        raise KeyError(scenario_id)

    scenario = SCENARIOS[scenario_id]
    request = _build_request(scenario)
    result = core_reconcile(request)

    received_events = sorted(
        scenario["events"],
        key=lambda item: (datetime.fromisoformat(item["received_at"]), item["event_id"]),
    )
    business_events = sorted(
        scenario["events"],
        key=lambda item: (datetime.fromisoformat(item["occurred_at"]), item["event_id"]),
    )

    anomalies = []
    for code in result.anomalies:
        title, detail = ANOMALY_COPY.get(code, (code, ""))
        anomalies.append({"code": code, "title": title, "detail": detail})

    repairs = []
    for code in result.repairs:
        title, detail = REPAIR_COPY.get(code, (code, ""))
        repairs.append({"code": code, "title": title, "detail": detail})

    return {
        "scenario_id": scenario_id,
        "title": scenario["title"],
        "tab_title": scenario["tab_title"],
        "tab_subtitle": scenario["tab_subtitle"],
        "customer": scenario["customer"],
        "problem": scenario["problem"],
        "order": scenario["order"],
        "business_impact": scenario["business_impact"],
        "before": scenario["before"],
        "after": scenario["after"],
        "received_order": [_ui_event(item) for item in received_events],
        "business_order": [_ui_event(item) for item in business_events],
        "anomalies": anomalies,
        "repairs": repairs,
        "core_state": _snapshot_label(result),
        "duplicate_event_ids": result.duplicate_event_ids,
        "evidence_event_ids": result.evidence_event_ids,
        "engine": "services/reconciler/app/engine.py",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


class DemoOrderCreate(BaseModel):
    store: str = Field(min_length=1, max_length=80)
    items: str = Field(min_length=1, max_length=160)
    total: int = Field(gt=0, le=1_000_000)
    pickup_at: str = Field(pattern=r"^\\d{2}:\\d{2}$")
    units: int = Field(ge=1, le=50)


class PaymentRequest(BaseModel):
    authorization_id: str | None = Field(default=None, max_length=80)


class CancelRequest(BaseModel):
    delay_seconds: int = Field(default=0, ge=0, le=600)


class AmountRequest(BaseModel):
    amount: int | None = Field(default=None, gt=0, le=1_000_000)


class CapacityRequest(BaseModel):
    available_units: int = Field(ge=0, le=100)


class RedeliveryRequest(BaseModel):
    conflicting_amount: int | None = Field(default=None, gt=0, le=1_000_000)


def _demo_error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail="demo session not found")
    return HTTPException(status_code=409, detail=str(exc))


@app.post("/api/demo/sessions")
def create_demo_session() -> dict[str, Any]:
    return demo_store.create_session()


@app.get("/api/demo/sessions/{session_id}")
def get_demo_session(session_id: str) -> dict[str, Any]:
    try:
        return demo_store.snapshot(session_id)
    except (KeyError, ValueError) as exc:
        raise _demo_error(exc) from exc


@app.post("/api/demo/sessions/{session_id}/reset")
def reset_demo_session(session_id: str) -> dict[str, Any]:
    try:
        return demo_store.reset(session_id)
    except (KeyError, ValueError) as exc:
        raise _demo_error(exc) from exc


@app.post("/api/demo/sessions/{session_id}/presets/{scenario_id}")
def load_demo_preset(session_id: str, scenario_id: str) -> dict[str, Any]:
    if scenario_id not in SCENARIOS:
        raise HTTPException(status_code=404, detail="unknown preset")
    try:
        return demo_store.seed_from_scenario(session_id, scenario_id, SCENARIOS[scenario_id])
    except (KeyError, ValueError) as exc:
        raise _demo_error(exc) from exc


@app.post("/api/demo/sessions/{session_id}/orders")
def create_demo_order(session_id: str, request: DemoOrderCreate) -> dict[str, Any]:
    try:
        return demo_store.create_order(
            session_id,
            store=request.store,
            items=request.items,
            total=request.total,
            pickup_at=request.pickup_at,
            units=request.units,
        )
    except (KeyError, ValueError) as exc:
        raise _demo_error(exc) from exc


@app.post("/api/demo/sessions/{session_id}/payment")
def authorize_demo_payment(session_id: str, request: PaymentRequest) -> dict[str, Any]:
    try:
        return demo_store.authorize_payment(session_id, request.authorization_id)
    except (KeyError, ValueError) as exc:
        raise _demo_error(exc) from exc


@app.post("/api/demo/sessions/{session_id}/confirm")
def confirm_demo_order(session_id: str) -> dict[str, Any]:
    try:
        return demo_store.confirm(session_id)
    except (KeyError, ValueError) as exc:
        raise _demo_error(exc) from exc


@app.post("/api/demo/sessions/{session_id}/cancel")
def cancel_demo_order(session_id: str, request: CancelRequest) -> dict[str, Any]:
    try:
        return demo_store.cancel(session_id, request.delay_seconds)
    except (KeyError, ValueError) as exc:
        raise _demo_error(exc) from exc


@app.post("/api/demo/sessions/{session_id}/settlement")
def settle_demo_order(session_id: str, request: AmountRequest) -> dict[str, Any]:
    try:
        return demo_store.settle(session_id, request.amount)
    except (KeyError, ValueError) as exc:
        raise _demo_error(exc) from exc


@app.post("/api/demo/sessions/{session_id}/reward")
def reward_demo_order(session_id: str, request: AmountRequest) -> dict[str, Any]:
    try:
        return demo_store.reward(session_id, request.amount)
    except (KeyError, ValueError) as exc:
        raise _demo_error(exc) from exc


@app.post("/api/demo/sessions/{session_id}/capacity")
def revise_demo_capacity(session_id: str, request: CapacityRequest) -> dict[str, Any]:
    try:
        return demo_store.revise_capacity(session_id, request.available_units)
    except (KeyError, ValueError) as exc:
        raise _demo_error(exc) from exc


@app.post("/api/demo/sessions/{session_id}/redelivery")
def redeliver_demo_event(session_id: str, request: RedeliveryRequest) -> dict[str, Any]:
    try:
        return demo_store.redeliver_last_financial(
            session_id,
            conflicting_amount=request.conflicting_amount,
        )
    except (KeyError, ValueError) as exc:
        raise _demo_error(exc) from exc


@app.post("/api/demo/sessions/{session_id}/reconcile")
def reconcile_demo_session(session_id: str) -> dict[str, Any]:
    try:
        return demo_store.reconcile(session_id)
    except (KeyError, ValueError) as exc:
        raise _demo_error(exc) from exc


@app.post("/api/demo/sessions/{session_id}/repairs/apply")
def apply_demo_repairs(session_id: str) -> dict[str, Any]:
    try:
        return demo_store.apply_repairs(session_id)
    except (KeyError, ValueError) as exc:
        raise _demo_error(exc) from exc


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return INDEX.read_text(encoding="utf-8")


@app.get("/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "pickup-pact-demo",
        "engine": "services/reconciler/app/engine.py",
    }


@app.get("/api/scenarios")
def list_scenarios() -> list[dict[str, str]]:
    return [
        {
            "id": key,
            "title": value["tab_title"],
            "subtitle": value["tab_subtitle"],
        }
        for key, value in SCENARIOS.items()
    ]


@app.get("/api/scenarios/{scenario_id}")
def scenario_endpoint(scenario_id: str) -> dict[str, Any]:
    try:
        return run_scenario(scenario_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="unknown scenario") from exc
