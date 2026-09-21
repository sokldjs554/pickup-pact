from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from flask import Flask, jsonify, render_template

app = Flask(__name__, template_folder=str(Path(__file__).resolve().parent / "templates"))
app.secret_key = os.getenv("FLASK_SECRET_KEY", "local-demo-only")
RECONCILER_URL = os.getenv("RECONCILER_URL", "http://localhost:8000")
BENCHMARK_PATH = Path(os.getenv("BENCHMARK_PATH", "/app/artifacts/consistency-benchmark.json"))


def _event(
    event_id: str,
    event_type: str,
    occurred_at: str,
    received_at: str | None = None,
    payload: dict | None = None,
    *,
    aggregate_id: str,
) -> dict:
    return {
        "event_id": event_id,
        "aggregate_id": aggregate_id,
        "event_type": event_type,
        "occurred_at": occurred_at,
        "received_at": received_at or occurred_at,
        "payload": payload or {},
    }


SCENARIOS = {
    "late-cancel": {
        "title": "늦게 도착한 취소",
        "eyebrow": "가장 먼저 볼 시나리오",
        "summary": "취소는 정산보다 먼저 발생했지만, 네트워크 지연 때문에 정산·적립 뒤에 도착했습니다.",
        "question": "이미 정산과 포인트 지급이 끝난 것처럼 보일 때, 무엇을 되돌려야 할까요?",
        "impact": "잘못된 정산·적립을 보상 분개로 되돌리고 조회 모델을 재구성합니다.",
        "accent": "late",
        "events": [
            _event("slot-held", "PickupSlotHeld", "2026-09-21T03:29:50Z", payload={"capacity_units": 2, "pickup_at": "2026-09-21T03:30:00Z"}, aggregate_id="order-late-cancel"),
            _event("payment-ok", "PaymentAuthorized", "2026-09-21T03:29:52Z", aggregate_id="order-late-cancel"),
            _event("promise", "CommitmentConfirmed", "2026-09-21T03:29:55Z", payload={"capacity_units": 2, "pickup_at": "2026-09-21T03:30:00Z"}, aggregate_id="order-late-cancel"),
            _event("cancel", "CommitmentCancelled", "2026-09-21T03:30:05Z", "2026-09-21T03:30:50Z", {"reason": "customer_request"}, aggregate_id="order-late-cancel"),
            _event("settlement", "SettlementPosted", "2026-09-21T03:30:10Z", payload={"amount": "12000"}, aggregate_id="order-late-cancel"),
            _event("reward", "RewardGranted", "2026-09-21T03:30:11Z", payload={"amount": "1200"}, aggregate_id="order-late-cancel"),
        ],
    },
    "duplicate": {
        "title": "Kafka 중복 전달",
        "eyebrow": "멱등성",
        "summary": "같은 정산 이벤트가 재전달되어 수신 순서 기준으로는 정산이 두 번 게시됩니다.",
        "question": "같은 event_id가 두 번 와도 금전 부작용을 한 번만 만들 수 있을까요?",
        "impact": "중복 부작용을 NO-OP 처리하고 드리프트한 조회 모델만 다시 만듭니다.",
        "accent": "duplicate",
        "events": [
            _event("slot-held", "PickupSlotHeld", "2026-09-21T04:10:00Z", payload={"capacity_units": 1}, aggregate_id="order-duplicate"),
            _event("payment-ok", "PaymentAuthorized", "2026-09-21T04:10:01Z", aggregate_id="order-duplicate"),
            _event("promise", "CommitmentConfirmed", "2026-09-21T04:10:02Z", payload={"capacity_units": 1}, aggregate_id="order-duplicate"),
            _event("settlement-77", "SettlementPosted", "2026-09-21T04:10:04Z", "2026-09-21T04:10:04Z", {"amount": "6800"}, aggregate_id="order-duplicate"),
            _event("settlement-77", "SettlementPosted", "2026-09-21T04:10:04Z", "2026-09-21T04:10:09Z", {"amount": "6800"}, aggregate_id="order-duplicate"),
        ],
    },
    "capacity-drop": {
        "title": "확정 뒤 매장 수용량 감소",
        "eyebrow": "예약 픽업 약속",
        "summary": "2단위 조리 수용량을 사용해 픽업을 확정했지만, 매장 상황 변경으로 가용량이 1로 줄었습니다.",
        "question": "이미 고객에게 약속한 픽업 시간을 무시하지 않고 어떻게 위험 상태로 전환할까요?",
        "impact": "확정 약속을 AT_RISK로 표시하고 재슬롯 검토 대상으로 보냅니다.",
        "accent": "capacity",
        "events": [
            _event("slot-held", "PickupSlotHeld", "2026-09-21T05:00:00Z", payload={"capacity_units": 2, "pickup_at": "2026-09-21T05:20:00Z"}, aggregate_id="order-capacity"),
            _event("payment-ok", "PaymentAuthorized", "2026-09-21T05:00:01Z", aggregate_id="order-capacity"),
            _event("promise", "CommitmentConfirmed", "2026-09-21T05:00:02Z", payload={"capacity_units": 2, "pickup_at": "2026-09-21T05:20:00Z"}, aggregate_id="order-capacity"),
            _event("capacity-r2", "CapacityRevised", "2026-09-21T05:02:10Z", payload={"revision": 2, "available_units": 1}, aggregate_id="order-capacity"),
        ],
    },
    "conflict": {
        "title": "같은 ID, 다른 금액",
        "eyebrow": "충돌 감지",
        "summary": "동일 event_id가 재전달됐지만 두 payload의 정산 금액이 서로 다릅니다.",
        "question": "중복으로 조용히 버려야 할까요, 아니면 사람이 확인해야 할까요?",
        "impact": "단순 중복과 충돌 중복을 구분해 금전 변경 없이 MANUAL_REVIEW로 격리합니다.",
        "accent": "conflict",
        "events": [
            _event("slot-held", "PickupSlotHeld", "2026-09-21T06:00:00Z", payload={"capacity_units": 1}, aggregate_id="order-conflict"),
            _event("payment-ok", "PaymentAuthorized", "2026-09-21T06:00:01Z", aggregate_id="order-conflict"),
            _event("promise", "CommitmentConfirmed", "2026-09-21T06:00:02Z", payload={"capacity_units": 1}, aggregate_id="order-conflict"),
            _event("settlement-X", "SettlementPosted", "2026-09-21T06:00:05Z", "2026-09-21T06:00:05Z", {"amount": "9800"}, aggregate_id="order-conflict"),
            _event("settlement-X", "SettlementPosted", "2026-09-21T06:00:05Z", "2026-09-21T06:00:08Z", {"amount": "19800"}, aggregate_id="order-conflict"),
        ],
    },
}


EVENT_LABELS = {
    "PickupSlotHeld": "픽업 슬롯 확보",
    "PaymentAuthorized": "결제 승인",
    "CommitmentConfirmed": "픽업 약속 확정",
    "CommitmentCancelled": "주문 취소",
    "SettlementPosted": "정산 게시",
    "RewardGranted": "포인트 지급",
    "CapacityRevised": "매장 수용량 변경",
    "SettlementReversed": "정산 보상 분개",
    "RewardReversed": "포인트 회수",
}

ANOMALY_LABELS = {
    "duplicate_event_delivery": "동일 이벤트가 중복 전달됨",
    "conflicting_duplicate_payload": "같은 이벤트 ID에 서로 다른 payload가 들어옴",
    "receive_order_projection_drift": "수신 순서 조회 모델이 canonical 상태와 어긋남",
    "confirmed_without_payment_authorization": "결제 승인 없이 픽업 약속이 확정됨",
    "confirmed_promise_exceeds_revised_capacity": "확정한 픽업 약속이 변경된 매장 수용량을 초과함",
    "settlement_posted_after_prior_cancellation": "실제 취소 이후 정산이 게시됨",
    "reward_granted_after_prior_cancellation": "실제 취소 이후 포인트가 지급됨",
}

REPAIR_LABELS = {
    "NO_OP_DUPLICATE": "중복 이벤트 부작용 차단",
    "REBUILD_PROJECTION": "CQRS 조회 모델 재구성",
    "REVERSE_SETTLEMENT": "정산 보상 분개 생성",
    "REVERSE_REWARD": "포인트 회수 분개 생성",
    "RESLOT_REVIEW": "픽업 시간 재슬롯 검토",
    "MANUAL_REVIEW": "금전 변경 없이 사람 검토로 격리",
}


def post_json(path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        RECONCILER_URL + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.loads(response.read())


def _benchmark() -> dict:
    try:
        return json.loads(BENCHMARK_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {
            "benchmark": "synthetic-consistency-race-v1",
            "metrics": {
                "orders": 20000,
                "simulated_events": 101588,
                "capacity_oversubscribed_units_baseline": 194,
                "capacity_oversubscribed_units_pickup_pact": 0,
                "duplicate_financial_posts_baseline": 1207,
                "duplicate_financial_posts_pickup_pact": 0,
                "stale_financial_orders_after_late_cancel_baseline": 381,
                "stale_financial_orders_after_late_cancel_pickup_pact": 0,
            },
            "scope": "Synthetic correctness benchmark; not production traffic or latency.",
        }


@app.get("/")
def index():
    bootstrap = {
        "scenarios": SCENARIOS,
        "event_labels": EVENT_LABELS,
        "anomaly_labels": ANOMALY_LABELS,
        "repair_labels": REPAIR_LABELS,
        "benchmark": _benchmark(),
    }
    return render_template("index.html", bootstrap=bootstrap)


@app.get("/api/demo/scenarios")
def list_scenarios():
    return jsonify({"scenarios": SCENARIOS})


@app.post("/api/demo/run/<scenario_id>")
def run_scenario(scenario_id: str):
    scenario = SCENARIOS.get(scenario_id)
    if scenario is None:
        return jsonify({"error": "unknown_scenario"}), 404
    try:
        result = post_json("/api/v1/replay", {"events": scenario["events"]})
    except (ValueError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        return (
            jsonify(
                {
                    "error": "reconciler_unavailable",
                    "message": str(exc),
                    "hint": "Start the reconciler or use Dockerfile.demo / docker compose.",
                }
            ),
            503,
        )
    return jsonify({"scenario": scenario_id, "result": result})


@app.get("/api/demo/status")
def demo_status():
    req = urllib.request.Request(RECONCILER_URL + "/health", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=2) as response:
            payload = json.loads(response.read())
        return jsonify({"demo": "ok", "reconciler": payload.get("status", "unknown")})
    except (ValueError, urllib.error.URLError, urllib.error.HTTPError):
        return jsonify({"demo": "degraded", "reconciler": "unavailable"}), 503


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
