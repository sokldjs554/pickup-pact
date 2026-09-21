from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

app = FastAPI(
    title="Pickup Pact Demo",
    version="1.0.0",
    description="Scheduled-pickup temporal consistency demo",
)

INDEX = Path(__file__).with_name("index.html")


class Event(BaseModel):
    event_id: str
    type: str
    occurred_at: str
    received_at: str
    detail: str
    amount: int | None = None


SCENARIOS: dict[str, dict[str, Any]] = {
    "late-cancel": {
        "title": "늦게 도착한 취소",
        "subtitle": "취소는 먼저 일어났지만 정산·적립 뒤에 도착했습니다.",
        "customer": "12:30 픽업 · 아메리카노 2잔 · 9,000원",
        "problem": "수신 순서만 믿으면 이미 취소된 주문에 정산과 포인트가 남습니다.",
        "events": [
            {"event_id": "evt-101", "type": "PICKUP_CONFIRMED", "occurred_at": "12:08:00", "received_at": "12:08:01", "detail": "12:30 픽업 약속 확정"},
            {"event_id": "evt-102", "type": "PAYMENT_AUTHORIZED", "occurred_at": "12:08:02", "received_at": "12:08:03", "detail": "결제 승인 9,000원", "amount": 9000},
            {"event_id": "evt-104", "type": "CANCELLED", "occurred_at": "12:14:10", "received_at": "12:15:02", "detail": "고객 취소 — 네트워크 지연으로 52초 늦게 수신"},
            {"event_id": "evt-103", "type": "SETTLED", "occurred_at": "12:14:35", "received_at": "12:14:36", "detail": "점주 정산 반영", "amount": 9000},
            {"event_id": "evt-105", "type": "REWARD_GRANTED", "occurred_at": "12:14:39", "received_at": "12:14:40", "detail": "포인트 90P 적립", "amount": 90},
        ],
    },
    "duplicate": {
        "title": "Kafka 중복 전달",
        "subtitle": "동일 결제 이벤트가 재전달돼도 금전 부작용은 한 번만 발생해야 합니다.",
        "customer": "13:00 픽업 · 라떼 1잔 · 5,500원",
        "problem": "at-least-once 전달에서 event_id 멱등성이 없으면 이중 정산이 발생합니다.",
        "events": [
            {"event_id": "evt-201", "type": "PICKUP_CONFIRMED", "occurred_at": "12:44:00", "received_at": "12:44:01", "detail": "13:00 픽업 확정"},
            {"event_id": "evt-202", "type": "SETTLED", "occurred_at": "12:44:05", "received_at": "12:44:06", "detail": "정산 5,500원", "amount": 5500},
            {"event_id": "evt-202", "type": "SETTLED", "occurred_at": "12:44:05", "received_at": "12:44:19", "detail": "Kafka redelivery — 동일 payload", "amount": 5500},
        ],
    },
    "capacity-drop": {
        "title": "매장 수용량 급감",
        "subtitle": "확정 후 제조 가능 수량이 줄어 픽업 약속을 지킬 수 없게 됐습니다.",
        "customer": "18:10 픽업 · 음료 4잔 · capacity 4→2",
        "problem": "주문 상태만 보면 CONFIRMED지만 실제 제조 슬롯은 이미 약속을 위반합니다.",
        "events": [
            {"event_id": "evt-301", "type": "CAPACITY_LEASED", "occurred_at": "17:48:00", "received_at": "17:48:00", "detail": "18:10 슬롯 4 units 임대"},
            {"event_id": "evt-302", "type": "PICKUP_CONFIRMED", "occurred_at": "17:48:03", "received_at": "17:48:04", "detail": "픽업 약속 확정"},
            {"event_id": "evt-303", "type": "CAPACITY_REVISED", "occurred_at": "17:55:00", "received_at": "17:55:01", "detail": "머신 장애로 available units 2", "amount": 2},
        ],
    },
    "conflict": {
        "title": "같은 event_id, 다른 금액",
        "subtitle": "단순 중복이 아니라 재사용된 이벤트 ID의 의미 충돌입니다.",
        "customer": "09:20 픽업 · 샌드위치 세트 · 11,500원",
        "problem": "event_id만 보고 중복 제거하면 금액 변조/상류 버그를 조용히 숨기게 됩니다.",
        "events": [
            {"event_id": "evt-401", "type": "SETTLED", "occurred_at": "09:01:00", "received_at": "09:01:01", "detail": "정산 11,500원", "amount": 11500},
            {"event_id": "evt-401", "type": "SETTLED", "occurred_at": "09:01:00", "received_at": "09:01:18", "detail": "동일 ID지만 정산 금액 13,500원", "amount": 13500},
        ],
    },
}


def _seconds(ts: str) -> int:
    h, m, s = (int(x) for x in ts.split(":"))
    return h * 3600 + m * 60 + s


def reconcile(scenario_id: str) -> dict[str, Any]:
    s = SCENARIOS[scenario_id]
    events = [Event(**e) for e in s["events"]]
    received = sorted(events, key=lambda e: (_seconds(e.received_at), e.event_id))
    canonical = sorted(events, key=lambda e: (_seconds(e.occurred_at), _seconds(e.received_at), e.event_id))

    anomalies: list[dict[str, str]] = []
    repairs: list[dict[str, str]] = []
    state_before = "CONFIRMED"
    state_after = "CONFIRMED"

    if scenario_id == "late-cancel":
        cancel = next(e for e in canonical if e.type == "CANCELLED")
        bad = [e for e in canonical if e.type in {"SETTLED", "REWARD_GRANTED"} and _seconds(e.occurred_at) > _seconds(cancel.occurred_at)]
        anomalies.append({
            "code": "LATE_FACT_CAUSALITY",
            "title": "수신 순서와 실제 발생 순서가 다름",
            "detail": f"취소가 {cancel.occurred_at}에 먼저 발생했지만 {cancel.received_at}에 늦게 도착했습니다."
        })
        if any(e.type == "SETTLED" for e in bad):
            repairs.append({"command": "REVERSE_SETTLEMENT", "why": "취소 이후 발생한 정산을 삭제하지 않고 보상 분개합니다."})
        if any(e.type == "REWARD_GRANTED" for e in bad):
            repairs.append({"command": "REVERSE_REWARD", "why": "취소 이후 적립된 포인트를 보상 이벤트로 회수합니다."})
        repairs.append({"command": "REBUILD_PROJECTION", "why": "event-time 기준으로 CQRS read model을 재구성합니다."})
        state_before, state_after = "SETTLED + REWARDED", "CANCELLED + COMPENSATED"

    elif scenario_id == "duplicate":
        fingerprints: dict[str, tuple[str, int | None]] = {}
        duplicate = False
        for e in received:
            fp = (e.type, e.amount)
            if e.event_id in fingerprints and fingerprints[e.event_id] == fp:
                duplicate = True
            fingerprints.setdefault(e.event_id, fp)
        if duplicate:
            anomalies.append({"code": "SAFE_REDELIVERY", "title": "동일 Kafka 이벤트 재전달", "detail": "event_id와 semantic fingerprint가 동일합니다."})
            repairs.append({"command": "NO_OP_DUPLICATE", "why": "ledger side effect를 두 번째로 만들지 않습니다."})
            repairs.append({"command": "VERIFY_PROJECTION", "why": "read model이 한 번만 반영됐는지 검증합니다."})
        state_before, state_after = "2 deliveries", "1 financial effect"

    elif scenario_id == "capacity-drop":
        anomalies.append({"code": "PROMISE_CAPACITY_DRIFT", "title": "확정 약속 > 현재 제조 수용량", "detail": "확정 당시 4 units였지만 현재 available capacity는 2입니다."})
        repairs.append({"command": "MARK_AT_RISK", "why": "기존 확정을 숨기지 않고 약속을 위험 상태로 전환합니다."})
        repairs.append({"command": "RESLOT_REVIEW", "why": "가장 가까운 대체 픽업 슬롯 검토를 요청합니다."})
        state_before, state_after = "CONFIRMED", "AT_RISK"

    elif scenario_id == "conflict":
        first, second = received
        if first.event_id == second.event_id and (first.type, first.amount) != (second.type, second.amount):
            anomalies.append({"code": "CONFLICTING_EVENT_REUSE", "title": "같은 event_id에 다른 의미", "detail": "11,500원과 13,500원이 같은 event_id로 들어왔습니다."})
            repairs.append({"command": "QUARANTINE_EVENT", "why": "안전한 redelivery로 처리하지 않고 격리합니다."})
            repairs.append({"command": "MANUAL_REVIEW", "why": "자동 금전 보정을 금지하고 근거 이벤트를 운영자에게 제시합니다."})
        state_before, state_after = "AMBIGUOUS", "QUARANTINED"

    return {
        "scenario_id": scenario_id,
        "title": s["title"],
        "subtitle": s["subtitle"],
        "customer": s["customer"],
        "problem": s["problem"],
        "received_order": [e.model_dump() for e in received],
        "canonical_order": [e.model_dump() for e in canonical],
        "anomalies": anomalies,
        "repairs": repairs,
        "state_before": state_before,
        "state_after": state_after,
        "evidence_event_ids": sorted({e.event_id for e in events}),
        "engine": "deterministic-temporal-reconciler/v1",
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return INDEX.read_text(encoding="utf-8")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "pickup-pact-demo"}


@app.get("/api/scenarios")
def list_scenarios() -> list[dict[str, str]]:
    return [
        {"id": key, "title": value["title"], "subtitle": value["subtitle"]}
        for key, value in SCENARIOS.items()
    ]


@app.get("/api/scenarios/{scenario_id}")
def run_scenario(scenario_id: str) -> dict[str, Any]:
    if scenario_id not in SCENARIOS:
        raise HTTPException(status_code=404, detail="unknown scenario")
    return reconcile(scenario_id)
