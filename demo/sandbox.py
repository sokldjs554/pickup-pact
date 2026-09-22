from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Any
from uuid import uuid4

from services.reconciler.app.engine import reconcile as core_reconcile
from services.reconciler.app.models import EventEnvelope, ReconcileRequest

EVENT_LABELS = {
    "PickupSlotHeld": "픽업 슬롯 확보",
    "PaymentAuthorized": "결제 승인",
    "CommitmentConfirmed": "픽업 주문 확정",
    "CommitmentCancelled": "고객 주문 취소",
    "SettlementPosted": "점주 정산 반영",
    "RewardGranted": "고객 포인트 적립",
    "CapacityRevised": "매장 처리량 변경",
    "PickupRescheduled": "픽업 시간 변경",
    "PickupPactIssued": "픽업 보장 발급",
    "PickupPactRenegotiated": "픽업 보장 재합의",
    "PickupPactBreached": "픽업 보장 위반",
    "PickupClaimed": "픽업 완료",
    "SettlementReversed": "정산 취소 분개",
    "RewardReversed": "포인트 회수",
}

ANOMALY_COPY = {
    "duplicate_event_delivery": (
        "같은 이벤트가 두 번 도착했습니다.",
        "동일 event_id가 재전달되었습니다. 금전 부작용은 다시 만들면 안 됩니다.",
    ),
    "conflicting_duplicate_payload": (
        "같은 이벤트 ID인데 내용이 다릅니다.",
        "안전한 재전달이 아니라 상류 오류 가능성이 있어 자동 반영을 중단해야 합니다.",
    ),
    "receive_order_projection_drift": (
        "서버가 받은 순서와 실제 발생 순서가 다릅니다.",
        "수신 순서로 만든 조회 상태가 실제 업무 발생 순서와 어긋났습니다.",
    ),
    "confirmed_without_payment_authorization": (
        "결제 승인 없이 픽업이 확정되었습니다.",
        "확정 전 반드시 충족되어야 하는 결제 승인 불변식이 깨졌습니다.",
    ),
    "confirmed_promise_exceeds_revised_capacity": (
        "확정한 픽업 약속을 현재 매장 처리량으로 지킬 수 없습니다.",
        "확정 당시보다 제조 가능량이 줄어 약속이 위험 상태가 되었습니다.",
    ),
    "settlement_posted_after_prior_cancellation": (
        "취소 뒤에 점주 정산이 반영되었습니다.",
        "실제 업무 시각 기준으로 취소가 정산보다 먼저 발생했습니다.",
    ),
    "reward_granted_after_prior_cancellation": (
        "취소 뒤에 포인트가 지급되었습니다.",
        "취소된 주문에 지급된 포인트를 회수해야 합니다.",
    ),
}

REPAIR_COPY = {
    "NO_OP_DUPLICATE": (
        "중복 금전 처리 차단",
        "같은 이벤트의 두 번째 전달은 부작용 없이 무시합니다.",
    ),
    "REBUILD_PROJECTION": (
        "조회 상태 재구성",
        "실제 발생 순서를 기준으로 고객/점주 화면용 projection을 다시 만듭니다.",
    ),
    "REVERSE_SETTLEMENT": (
        "점주 정산 보상 분개",
        "기존 정산 기록을 삭제하지 않고 반대 분개를 추가합니다.",
    ),
    "REVERSE_REWARD": (
        "포인트 회수",
        "취소 이후 지급된 포인트를 별도 reversal 이벤트로 회수합니다.",
    ),
    "RESLOT_REVIEW": (
        "대체 픽업 시간 검토",
        "현재 매장 처리량으로 지킬 수 있는 다른 슬롯을 검토 대상으로 올립니다.",
    ),
    "MANUAL_REVIEW": (
        "자동 처리 중단",
        "이벤트 의미가 충돌하므로 금전 상태를 자동 변경하지 않고 운영자 검토로 보냅니다.",
    ),
}


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


def _money(value: Any) -> int:
    if value is None:
        return 0
    return int(str(value).replace(",", "").replace("원", "").strip())


def _shift_hhmm(value: str, minutes: int) -> str:
    hour, minute = (int(part) for part in value.split(":", 1))
    total = (hour * 60 + minute + minutes) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def _new_pickup_code() -> str:
    return f"{int(uuid4().hex[:8], 16) % 10000:04d}"


def _empty_pickup_pact() -> dict[str, Any]:
    return {
        "status": "NONE",
        "promised_at": None,
        "latest_at": None,
        "compensation_points": 0,
        "compensation_granted": False,
        "version": 0,
    }


def _empty_merchant_fulfillment() -> dict[str, Any]:
    return {
        "status": "NONE",
        "delivery_sequence": 0,
        "delivery_acknowledged": False,
        "redelivery_count": 0,
        "effects": [],
        "anomalies": [],
        "window": None,
        "ready_quality": None,
    }


def _merchant_window(pickup_at: str, units: int) -> dict[str, Any]:
    prep_minutes = max(1, min(10, (units * 45 + 59) // 60))
    target_ready = _shift_hhmm(pickup_at, -1)
    earliest_start = _shift_hhmm(target_ready, -(prep_minutes + 2))
    latest_ready = _shift_hhmm(pickup_at, 3)
    return {
        "preparation_minutes": prep_minutes,
        "earliest_start_at": earliest_start,
        "target_ready_at": target_ready,
        "latest_ready_at": latest_ready,
    }


class DemoStore:
    """Thread-safe, session-isolated in-memory sandbox for the public portfolio demo.

    It intentionally models operator actions and evidence only. Production persistence
    remains in the real service modules; the sandbox exists so interviewers can exercise
    the domain flow without requiring Kafka/PostgreSQL/Redis on Render.
    """

    def __init__(self, max_sessions: int = 100) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}
        self._lock = RLock()
        self._max_sessions = max_sessions

    def _prune(self) -> None:
        if len(self._sessions) < self._max_sessions:
            return
        oldest = sorted(
            self._sessions.items(),
            key=lambda item: item[1]["created_at"],
        )[: max(1, self._max_sessions // 10)]
        for session_id, _ in oldest:
            self._sessions.pop(session_id, None)

    def create_session(self) -> dict[str, Any]:
        with self._lock:
            self._prune()
            session_id = uuid4().hex
            now = _now()
            self._sessions[session_id] = {
                "session_id": session_id,
                "created_at": now,
                "updated_at": now,
                "order": None,
                "capacity": {
                    "slot": "12:30",
                    "available_units": 12,
                    "reserved_units": 0,
                    "revision": 1,
                },
                "events": [],
                "ledger_batches": [],
                "audit": [],
                "applied_repairs": [],
                "manual_review": False,
                "projection_rebuilds": 0,
                "pickup_protection": {
                    "status": "NONE",
                    "original_pickup_at": None,
                    "suggested_pickup_at": None,
                },
                "pickup_pact": _empty_pickup_pact(),
                "merchant_fulfillment": _empty_merchant_fulfillment(),
                "customer_history": [],
            }
            self._audit_locked(session_id, "SESSION_CREATED", "새 데모 세션을 만들었습니다.")
            return self.snapshot(session_id)

    def require(self, session_id: str) -> dict[str, Any]:
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        return session

    def reset(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            current = self.require(session_id)
            created_at = current["created_at"]
            self._sessions[session_id] = {
                "session_id": session_id,
                "created_at": created_at,
                "updated_at": _now(),
                "order": None,
                "capacity": {
                    "slot": "12:30",
                    "available_units": 12,
                    "reserved_units": 0,
                    "revision": 1,
                },
                "events": [],
                "ledger_batches": [],
                "audit": [],
                "applied_repairs": [],
                "manual_review": False,
                "projection_rebuilds": 0,
                "pickup_protection": {
                    "status": "NONE",
                    "original_pickup_at": None,
                    "suggested_pickup_at": None,
                },
                "pickup_pact": _empty_pickup_pact(),
                "merchant_fulfillment": _empty_merchant_fulfillment(),
                "customer_history": [],
            }
            self._audit_locked(session_id, "SESSION_RESET", "데모 상태를 초기화했습니다.")
            return self.snapshot(session_id)

    def _touch(self, session: dict[str, Any]) -> None:
        session["updated_at"] = _now()

    def _audit_locked(self, session_id: str, action: str, detail: str) -> None:
        session = self.require(session_id)
        session["audit"].append(
            {
                "id": uuid4().hex[:10],
                "action": action,
                "detail": detail,
                "at": _iso(_now()),
            }
        )
        self._touch(session)

    def _append_event_locked(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        detail: str,
        occurred_at: datetime | None = None,
        received_at: datetime | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        session = self.require(session_id)
        order = session.get("order")
        if not order:
            raise ValueError("order is required before emitting domain events")
        occurred = occurred_at or _now()
        received = received_at or occurred
        event = {
            "event_id": event_id or f"evt-{uuid4().hex[:12]}",
            "aggregate_id": order["aggregate_id"],
            "event_type": event_type,
            "occurred_at": _iso(occurred),
            "received_at": _iso(received),
            "payload": payload or {},
            "detail": detail,
        }
        session["events"].append(event)
        self._touch(session)
        return event

    def _ledger_locked(
        self,
        session_id: str,
        posting_type: str,
        amount: int,
        source_event_id: str,
    ) -> dict[str, Any]:
        session = self.require(session_id)
        accounts = {
            "SETTLEMENT": ("platform-clearing", "merchant-payable"),
            "REVERSE_SETTLEMENT": ("merchant-payable", "platform-clearing"),
            "REWARD": ("reward-expense", "customer-reward-liability"),
            "REVERSE_REWARD": ("customer-reward-liability", "reward-expense"),
        }
        debit, credit = accounts[posting_type]
        batch = {
            "batch_id": f"ledger-{uuid4().hex[:10]}",
            "source_event_id": source_event_id,
            "posting_type": posting_type,
            "amount": amount,
            "currency": "PTS" if posting_type in {"REWARD", "REVERSE_REWARD"} else "KRW",
            "debit_account": debit,
            "credit_account": credit,
            "created_at": _iso(_now()),
        }
        session["ledger_batches"].append(batch)
        self._touch(session)
        return batch

    def _customer_timeline_locked(self, session_id: str) -> list[dict[str, Any]]:
        session = self.require(session_id)
        labels = {
            "PickupSlotHeld": "주문 접수",
            "PaymentAuthorized": "결제 완료",
            "CommitmentConfirmed": "매장 확인",
            "PickupRescheduled": "픽업 시간 변경",
            "PickupPactIssued": "픽업 보장 시작",
            "PickupPactRenegotiated": "픽업 보장 변경",
            "PickupPactBreached": "픽업 보상 자동 적용",
            "PickupClaimed": "픽업 완료",
            "CommitmentCancelled": "주문 취소",
            "RewardGranted": "포인트 적립",
            "SettlementReversed": "결제 취소 완료",
            "RewardReversed": "포인트 조정 완료",
        }
        timeline: list[dict[str, Any]] = []
        for event in sorted(session["events"], key=lambda item: (item["occurred_at"], item["event_id"])):
            label = labels.get(event["event_type"])
            if not label:
                continue
            detail = event["detail"]
            if event["event_type"] == "PickupRescheduled":
                detail = f"{event['payload'].get('from_pickup_at')} → {event['payload'].get('pickup_at')}"
            elif event["event_type"] == "PickupPactIssued":
                detail = f"{event['payload'].get('promised_at')}~{event['payload'].get('latest_at')} 보장 · 초과 시 {event['payload'].get('compensation_points')}P"
            elif event["event_type"] == "PickupPactRenegotiated":
                detail = f"{event['payload'].get('promised_at')}~{event['payload'].get('latest_at')} 새 보장"
            elif event["event_type"] == "PickupPactBreached":
                detail = f"보장 시간 초과 · {event['payload'].get('compensation_points')}P 자동 보상"
            elif event["event_type"] == "PickupClaimed":
                detail = "매장에서 수령 확인"
            elif event["event_type"] == "SettlementReversed":
                detail = "결제 금액 정리 완료"
            timeline.append({
                "type": event["event_type"],
                "label": label,
                "detail": detail,
                "at": event["occurred_at"],
            })
        return timeline

    def _customer_receipt_locked(self, session_id: str) -> dict[str, Any]:
        session = self.require(session_id)
        order = session.get("order")
        if not order:
            raise ValueError("order not found")
        settlement = sum(
            batch["amount"] if batch["posting_type"] == "SETTLEMENT"
            else -batch["amount"] if batch["posting_type"] == "REVERSE_SETTLEMENT"
            else 0
            for batch in session["ledger_batches"]
        )
        reward = sum(
            batch["amount"] if batch["posting_type"] == "REWARD"
            else -batch["amount"] if batch["posting_type"] == "REVERSE_REWARD"
            else 0
            for batch in session["ledger_batches"]
        )
        terminal = order["status"] in {"CANCELLED", "PICKED_UP"}
        final_charge = 0 if order["status"] == "CANCELLED" else order["total"]
        return {
            "order_id": order["order_id"],
            "store": order["store"],
            "items": order["items"],
            "total": order["total"],
            "status": order["status"],
            "pickup_at": order["pickup_at"],
            "pickup_code": order.get("pickup_code"),
            "pickup_claimed": bool(order.get("pickup_claimed")),
            "payment_authorized": bool(order.get("payment_authorized")),
            "final_charge": final_charge,
            "settlement_balance": settlement,
            "reward_balance": reward,
            "pickup_pact": deepcopy(session["pickup_pact"]),
            "terminal": terminal,
            "timeline": self._customer_timeline_locked(session_id),
            "updated_at": _iso(session["updated_at"]),
        }

    def _archive_customer_order_locked(self, session_id: str) -> dict[str, Any]:
        session = self.require(session_id)
        receipt = self._customer_receipt_locked(session_id)
        if not receipt["terminal"]:
            raise ValueError("only terminal customer orders can be archived")
        summary = {
            "order_id": receipt["order_id"],
            "store": receipt["store"],
            "items": receipt["items"],
            "total": receipt["total"],
            "status": receipt["status"],
            "pickup_at": receipt["pickup_at"],
            "final_charge": receipt["final_charge"],
            "updated_at": receipt["updated_at"],
            "receipt": receipt,
        }
        history = session["customer_history"]
        for index, item in enumerate(history):
            if item["order_id"] == summary["order_id"]:
                history[index] = summary
                break
        else:
            history.insert(0, summary)
        self._touch(session)
        return deepcopy(summary)

    def customer_history(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy(self.require(session_id)["customer_history"])

    def customer_receipt(self, session_id: str, order_id: str) -> dict[str, Any]:
        with self._lock:
            session = self.require(session_id)
            if session.get("order") and session["order"]["order_id"] == order_id:
                return deepcopy(self._customer_receipt_locked(session_id))
            for item in session["customer_history"]:
                if item["order_id"] == order_id:
                    return deepcopy(item["receipt"])
            raise KeyError(order_id)

    def create_order(
        self,
        session_id: str,
        *,
        store: str,
        items: str,
        total: int,
        pickup_at: str,
        units: int,
    ) -> dict[str, Any]:
        with self._lock:
            session = self.require(session_id)
            if session["order"] is not None:
                raise ValueError("reset the demo before creating another order in this session")
            if units < 1:
                raise ValueError("units must be positive")
            if total < 1:
                raise ValueError("total must be positive")
            if units > session["capacity"]["available_units"]:
                raise ValueError("requested units exceed current slot capacity")

            aggregate_id = f"order-{uuid4().hex[:10]}"
            order_id = f"PP-{uuid4().hex[:6].upper()}"
            session["order"] = {
                "order_id": order_id,
                "aggregate_id": aggregate_id,
                "store": store,
                "items": items,
                "total": total,
                "pickup_at": pickup_at,
                "units": units,
                "status": "HELD",
                "payment_authorized": False,
                "settled": False,
                "rewarded": False,
                "pickup_code": None,
                "pickup_claimed": False,
            }
            session["capacity"]["slot"] = pickup_at
            session["capacity"]["reserved_units"] += units
            session["pickup_protection"] = {
                "status": "ON_TIME",
                "original_pickup_at": pickup_at,
                "suggested_pickup_at": None,
            }
            event = self._append_event_locked(
                session_id,
                "PickupSlotHeld",
                {"capacity_units": units, "pickup_at": pickup_at},
                detail=f"{pickup_at} 픽업 슬롯 {units} units 확보",
            )
            self._audit_locked(
                session_id,
                "ORDER_HELD",
                f"{order_id} · {items} · {total:,}원 · {pickup_at} 픽업",
            )
            return {"order": deepcopy(session["order"]), "event": deepcopy(event), "state": self.snapshot(session_id)}

    def authorize_payment(self, session_id: str, authorization_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            session = self.require(session_id)
            order = session.get("order")
            if not order:
                raise ValueError("order not found")
            if order["payment_authorized"]:
                raise ValueError("payment is already authorized")
            if order["status"] != "HELD":
                raise ValueError("payment authorization is allowed only for HELD orders")
            order["payment_authorized"] = True
            auth_id = authorization_id or f"auth-{uuid4().hex[:10]}"
            event = self._append_event_locked(
                session_id,
                "PaymentAuthorized",
                {"authorization_id": auth_id, "amount": str(order["total"])},
                detail=f"결제 {order['total']:,}원 승인",
            )
            self._audit_locked(session_id, "PAYMENT_AUTHORIZED", f"authorization={auth_id}")
            return {"event": deepcopy(event), "state": self.snapshot(session_id)}

    def confirm(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            session = self.require(session_id)
            order = session.get("order")
            if not order:
                raise ValueError("order not found")
            if order["status"] != "HELD":
                raise ValueError("only HELD orders can be confirmed")
            if not order["payment_authorized"]:
                raise ValueError("payment authorization is required")
            order["status"] = "CONFIRMED"
            if not order.get("pickup_code"):
                order["pickup_code"] = _new_pickup_code()
            event = self._append_event_locked(
                session_id,
                "CommitmentConfirmed",
                {"capacity_units": order["units"], "pickup_at": order["pickup_at"]},
                detail=f"{order['pickup_at']} 픽업 약속 확정",
            )
            compensation_points = 500
            latest_at = _shift_hhmm(order["pickup_at"], 3)
            session["pickup_pact"] = {
                "status": "ACTIVE",
                "promised_at": order["pickup_at"],
                "latest_at": latest_at,
                "compensation_points": compensation_points,
                "compensation_granted": False,
                "version": 1,
            }
            self._append_event_locked(
                session_id,
                "PickupPactIssued",
                {
                    "promised_at": order["pickup_at"],
                    "latest_at": latest_at,
                    "compensation_points": compensation_points,
                    "version": 1,
                },
                detail=f"{order['pickup_at']}~{latest_at} 보장 · 초과 시 {compensation_points}P 자동 보상",
            )
            self._audit_locked(
                session_id,
                "PICKUP_PACT_ISSUED",
                f"{order['pickup_at']}~{latest_at} · {compensation_points}P",
            )
            merchant = {
                "status": "RECEIVED",
                "delivery_sequence": 1,
                "delivery_acknowledged": False,
                "redelivery_count": 0,
                "effects": ["NEW_ORDER_NOTIFICATION", "POS_PRINT"],
                "anomalies": [],
                "window": _merchant_window(order["pickup_at"], order["units"]),
                "ready_quality": None,
            }
            session["merchant_fulfillment"] = merchant
            self._audit_locked(
                session_id,
                "MERCHANT_ORDER_RECEIVED",
                "점주 delivery 생성 · 알림/POS effect exactly-once",
            )
            self._audit_locked(session_id, "ORDER_CONFIRMED", f"{order['order_id']} 픽업 확정")
            return {"event": deepcopy(event), "state": self.snapshot(session_id)}

    def cancel(self, session_id: str, delay_seconds: int = 0) -> dict[str, Any]:
        with self._lock:
            session = self.require(session_id)
            order = session.get("order")
            if not order:
                raise ValueError("order not found")
            if order["status"] == "CANCELLED":
                raise ValueError("order is already cancelled")
            if order["status"] not in {"HELD", "CONFIRMED", "AT_RISK"}:
                raise ValueError("order cannot be cancelled from its current state")

            occurred = _now()
            received = occurred + timedelta(seconds=max(0, delay_seconds))
            order["status"] = "CANCELLED"
            session["capacity"]["reserved_units"] = max(
                0,
                session["capacity"]["reserved_units"] - order["units"],
            )
            protection = session.get("pickup_protection") or {}
            session["pickup_protection"] = {
                "status": "CANCELLED",
                "original_pickup_at": protection.get("original_pickup_at") or order["pickup_at"],
                "suggested_pickup_at": protection.get("suggested_pickup_at"),
            }
            if session["pickup_pact"]["status"] in {"ACTIVE", "COMPENSATED"}:
                session["pickup_pact"]["status"] = "CANCELLED"
            merchant = session.get("merchant_fulfillment") or _empty_merchant_fulfillment()
            if merchant.get("status") in {"RECEIVED", "ACCEPTED"}:
                merchant["status"] = "CANCELLED"
            elif merchant.get("status") in {"PREPARING", "READY"}:
                merchant["status"] = "CANCELLATION_REVIEW"
                if "CANCEL_AFTER_PREPARATION" not in merchant["anomalies"]:
                    merchant["anomalies"].append("CANCEL_AFTER_PREPARATION")
            session["merchant_fulfillment"] = merchant
            event = self._append_event_locked(
                session_id,
                "CommitmentCancelled",
                {"reason": "customer_request"},
                detail=(
                    "고객 주문 취소"
                    if delay_seconds <= 0
                    else f"고객 주문 취소 · 메시지 {delay_seconds}초 지연"
                ),
                occurred_at=occurred,
                received_at=received,
            )
            self._audit_locked(
                session_id,
                "ORDER_CANCELLED",
                f"{order['order_id']} · delivery_delay={max(0, delay_seconds)}s",
            )
            return {"event": deepcopy(event), "state": self.snapshot(session_id)}

    def settle(self, session_id: str, amount: int | None = None) -> dict[str, Any]:
        with self._lock:
            session = self.require(session_id)
            order = session.get("order")
            if not order:
                raise ValueError("order not found")
            actual = amount or order["total"]
            event = self._append_event_locked(
                session_id,
                "SettlementPosted",
                {"amount": str(actual)},
                detail=f"점주 정산 {actual:,}원 반영",
            )
            order["settled"] = True
            batch = self._ledger_locked(session_id, "SETTLEMENT", actual, event["event_id"])
            self._audit_locked(session_id, "SETTLEMENT_POSTED", f"{actual:,}원")
            return {"event": deepcopy(event), "ledger": deepcopy(batch), "state": self.snapshot(session_id)}

    def reward(self, session_id: str, amount: int | None = None) -> dict[str, Any]:
        with self._lock:
            session = self.require(session_id)
            order = session.get("order")
            if not order:
                raise ValueError("order not found")
            actual = amount if amount is not None else max(1, order["total"] // 100)
            event = self._append_event_locked(
                session_id,
                "RewardGranted",
                {"amount": str(actual)},
                detail=f"고객 포인트 {actual}P 적립",
            )
            order["rewarded"] = True
            batch = self._ledger_locked(session_id, "REWARD", actual, event["event_id"])
            self._audit_locked(session_id, "REWARD_GRANTED", f"{actual}P")
            return {"event": deepcopy(event), "ledger": deepcopy(batch), "state": self.snapshot(session_id)}

    def revise_capacity(self, session_id: str, available_units: int) -> dict[str, Any]:
        with self._lock:
            session = self.require(session_id)
            order = session.get("order")
            if not order:
                raise ValueError("order not found")
            if available_units < 0:
                raise ValueError("available_units must be non-negative")
            session["capacity"]["revision"] += 1
            session["capacity"]["available_units"] = available_units
            event = self._append_event_locked(
                session_id,
                "CapacityRevised",
                {
                    "revision": session["capacity"]["revision"],
                    "available_units": available_units,
                },
                detail=f"매장 처리 가능량 변경 → {available_units} units",
            )
            self._audit_locked(session_id, "CAPACITY_REVISED", f"available_units={available_units}")
            return {"event": deepcopy(event), "state": self.snapshot(session_id)}

    def redeliver_last_financial(
        self,
        session_id: str,
        *,
        conflicting_amount: int | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            session = self.require(session_id)
            candidates = [
                event
                for event in session["events"]
                if event["event_type"] in {"SettlementPosted", "RewardGranted"}
            ]
            if not candidates:
                raise ValueError("no financial event exists to redeliver")
            source = candidates[-1]
            payload = deepcopy(source["payload"])
            if conflicting_amount is not None:
                payload["amount"] = str(conflicting_amount)
            event = self._append_event_locked(
                session_id,
                source["event_type"],
                payload,
                detail=(
                    "동일 이벤트 재전달"
                    if conflicting_amount is None
                    else f"동일 event_id 재사용 · amount={conflicting_amount}"
                ),
                occurred_at=datetime.fromisoformat(source["occurred_at"]),
                received_at=_now() + timedelta(seconds=1),
                event_id=source["event_id"],
            )
            self._audit_locked(
                session_id,
                "EVENT_REDELIVERED",
                (
                    f"{source['event_id']} exact duplicate"
                    if conflicting_amount is None
                    else f"{source['event_id']} conflicting amount={conflicting_amount}"
                ),
            )
            return {"event": deepcopy(event), "state": self.snapshot(session_id)}

    def _reconcile_locked(self, session_id: str) -> dict[str, Any]:
        session = self.require(session_id)
        if not session["events"]:
            return {
                "aggregate_id": None,
                "receive_order_state": None,
                "canonical_state": None,
                "anomalies": [],
                "repairs": [],
                "duplicate_event_ids": [],
                "evidence_event_ids": [],
            }
        request = ReconcileRequest(
            events=[
                EventEnvelope(
                    event_id=item["event_id"],
                    aggregate_id=item["aggregate_id"],
                    event_type=item["event_type"],
                    occurred_at=item["occurred_at"],
                    received_at=item["received_at"],
                    payload=item["payload"],
                )
                for item in session["events"]
            ]
        )
        result = core_reconcile(request)
        anomalies = []
        for code in result.anomalies:
            title, detail = ANOMALY_COPY.get(code, (code, ""))
            anomalies.append({"code": code, "title": title, "detail": detail})
        repairs = []
        for code in result.repairs:
            title, detail = REPAIR_COPY.get(code, (code, ""))
            repairs.append(
                {
                    "code": code,
                    "title": title,
                    "detail": detail,
                    "applied": code in session["applied_repairs"],
                }
            )
        return {
            "aggregate_id": result.aggregate_id,
            "receive_order_state": result.receive_order_state.model_dump(mode="json"),
            "canonical_state": result.canonical_state.model_dump(mode="json"),
            "anomalies": anomalies,
            "repairs": repairs,
            "duplicate_event_ids": result.duplicate_event_ids,
            "evidence_event_ids": result.evidence_event_ids,
        }

    def reconcile(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            result = self._reconcile_locked(session_id)
            self._audit_locked(
                session_id,
                "RECONCILED",
                f"{len(result['anomalies'])} anomalies · {len(result['repairs'])} repair proposals",
            )
            return {"reconciliation": deepcopy(result), "state": self.snapshot(session_id)}

    def apply_repairs(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            session = self.require(session_id)
            order = session.get("order")
            if not order:
                raise ValueError("order not found")
            result = self._reconcile_locked(session_id)
            applied: list[str] = []

            for repair in result["repairs"]:
                code = repair["code"]
                if code in session["applied_repairs"]:
                    continue
                if code == "REVERSE_SETTLEMENT":
                    original = next(
                        (
                            event
                            for event in reversed(session["events"])
                            if event["event_type"] == "SettlementPosted"
                        ),
                        None,
                    )
                    if original:
                        amount = _money(original["payload"].get("amount"))
                        event = self._append_event_locked(
                            session_id,
                            "SettlementReversed",
                            {"amount": str(amount), "source_event_id": original["event_id"]},
                            detail=f"정산 {amount:,}원 보상 분개",
                        )
                        self._ledger_locked(
                            session_id,
                            "REVERSE_SETTLEMENT",
                            amount,
                            event["event_id"],
                        )
                        order["settled"] = False
                        applied.append(code)
                elif code == "REVERSE_REWARD":
                    original = next(
                        (
                            event
                            for event in reversed(session["events"])
                            if event["event_type"] == "RewardGranted"
                        ),
                        None,
                    )
                    if original:
                        amount = _money(original["payload"].get("amount"))
                        event = self._append_event_locked(
                            session_id,
                            "RewardReversed",
                            {"amount": str(amount), "source_event_id": original["event_id"]},
                            detail=f"포인트 {amount}P 회수",
                        )
                        self._ledger_locked(
                            session_id,
                            "REVERSE_REWARD",
                            amount,
                            event["event_id"],
                        )
                        order["rewarded"] = False
                        applied.append(code)
                elif code == "REBUILD_PROJECTION":
                    session["projection_rebuilds"] += 1
                    applied.append(code)
                elif code == "RESLOT_REVIEW":
                    order["status"] = "AT_RISK"
                    original_pickup_at = order["pickup_at"]
                    suggested_pickup_at = _shift_hhmm(original_pickup_at, 5)
                    session["pickup_protection"] = {
                        "status": "SUGGESTED",
                        "original_pickup_at": original_pickup_at,
                        "suggested_pickup_at": suggested_pickup_at,
                    }
                    self._audit_locked(
                        session_id,
                        "PICKUP_RESLOT_SUGGESTED",
                        f"{original_pickup_at} → {suggested_pickup_at}",
                    )
                    applied.append(code)
                elif code == "NO_OP_DUPLICATE":
                    applied.append(code)
                elif code == "MANUAL_REVIEW":
                    session["manual_review"] = True
                    applied.append(code)

            for code in applied:
                if code not in session["applied_repairs"]:
                    session["applied_repairs"].append(code)
            if applied:
                self._audit_locked(
                    session_id,
                    "REPAIR_PLAN_APPLIED_IN_SANDBOX",
                    ", ".join(applied),
                )
            if order["status"] == "CANCELLED":
                self._archive_customer_order_locked(session_id)
            after = self._reconcile_locked(session_id)
            return {
                "applied": applied,
                "reconciliation": deepcopy(after),
                "state": self.snapshot(session_id),
            }

    def breach_pickup_pact(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            session = self.require(session_id)
            order = session.get("order")
            if not order:
                raise ValueError("order not found")
            if order["status"] != "CONFIRMED":
                raise ValueError("pickup pact can be breached only for a confirmed order")
            pact = session.get("pickup_pact") or {}
            if pact.get("status") != "ACTIVE":
                raise ValueError("no active pickup pact")
            if pact.get("compensation_granted"):
                raise ValueError("pickup pact compensation already granted")

            points = int(pact.get("compensation_points") or 500)
            breach_event = self._append_event_locked(
                session_id,
                "PickupPactBreached",
                {
                    "promised_at": pact["promised_at"],
                    "latest_at": pact["latest_at"],
                    "compensation_points": points,
                    "version": pact["version"],
                },
                detail=f"{pact['latest_at']} 보장 초과 · {points}P 자동 보상",
            )
            reward_event = self._append_event_locked(
                session_id,
                "RewardGranted",
                {"amount": str(points), "source": "pickup_pact_breach"},
                detail=f"픽업 보장 {points}P 자동 보상",
            )
            self._ledger_locked(
                session_id,
                "REWARD",
                points,
                reward_event["event_id"],
            )
            order["rewarded"] = True
            session["pickup_pact"] = {
                **pact,
                "status": "COMPENSATED",
                "compensation_granted": True,
            }
            self._audit_locked(
                session_id,
                "PICKUP_PACT_COMPENSATED",
                f"{breach_event['event_id']} · {points}P",
            )
            return {
                "event": deepcopy(breach_event),
                "reward_event": deepcopy(reward_event),
                "state": self.snapshot(session_id),
            }

    def claim_pickup(self, session_id: str, pickup_code: str) -> dict[str, Any]:
        with self._lock:
            session = self.require(session_id)
            order = session.get("order")
            if not order:
                raise ValueError("order not found")
            if order.get("pickup_claimed"):
                raise ValueError("pickup code has already been used")
            if order["status"] != "CONFIRMED":
                raise ValueError("pickup is allowed only for a confirmed order")
            if not order.get("pickup_code") or order["pickup_code"] != pickup_code:
                raise ValueError("pickup code does not match")

            event = self._append_event_locked(
                session_id,
                "PickupClaimed",
                {"pickup_code": pickup_code},
                detail="1회용 픽업 코드 확인",
            )
            order["pickup_claimed"] = True
            order["status"] = "PICKED_UP"
            merchant = session.get("merchant_fulfillment") or _empty_merchant_fulfillment()
            merchant["status"] = "PICKED_UP"
            session["merchant_fulfillment"] = merchant
            if session["pickup_pact"]["status"] == "ACTIVE":
                session["pickup_pact"]["status"] = "FULFILLED"
            session["capacity"]["reserved_units"] = max(
                0,
                session["capacity"]["reserved_units"] - order["units"],
            )
            settlement_event = self._append_event_locked(
                session_id,
                "SettlementPosted",
                {"amount": str(order["total"]), "source": "pickup_claim"},
                detail=f"점주 정산 {order['total']:,}원 반영",
            )
            order["settled"] = True
            self._ledger_locked(
                session_id,
                "SETTLEMENT",
                order["total"],
                settlement_event["event_id"],
            )
            reward_amount = max(1, order["total"] // 100)
            reward_event = self._append_event_locked(
                session_id,
                "RewardGranted",
                {"amount": str(reward_amount), "source": "pickup_claim"},
                detail=f"고객 포인트 {reward_amount}P 적립",
            )
            order["rewarded"] = True
            self._ledger_locked(
                session_id,
                "REWARD",
                reward_amount,
                reward_event["event_id"],
            )
            self._audit_locked(
                session_id,
                "PICKUP_CLAIMED",
                f"{order['order_id']} · one-time code consumed",
            )
            history = self._archive_customer_order_locked(session_id)
            return {
                "event": deepcopy(event),
                "history": history,
                "state": self.snapshot(session_id),
            }

    def next_customer_order(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            current = self.require(session_id)
            if current.get("order") and current["order"]["status"] not in {"CANCELLED", "PICKED_UP"}:
                raise ValueError("current order is not complete")
            history = deepcopy(current["customer_history"])
            created_at = current["created_at"]
            self._sessions[session_id] = {
                "session_id": session_id,
                "created_at": created_at,
                "updated_at": _now(),
                "order": None,
                "capacity": {
                    "slot": "12:30",
                    "available_units": 12,
                    "reserved_units": 0,
                    "revision": 1,
                },
                "events": [],
                "ledger_batches": [],
                "audit": [],
                "applied_repairs": [],
                "manual_review": False,
                "projection_rebuilds": 0,
                "pickup_protection": {
                    "status": "NONE",
                    "original_pickup_at": None,
                    "suggested_pickup_at": None,
                },
                "pickup_pact": _empty_pickup_pact(),
                "merchant_fulfillment": _empty_merchant_fulfillment(),
                "customer_history": history,
            }
            self._audit_locked(session_id, "CUSTOMER_NEXT_ORDER", "이전 주문 내역을 보존하고 새 주문을 시작했습니다.")
            return self.snapshot(session_id)

    def accept_pickup_reschedule(self, session_id: str, pickup_at: str | None = None) -> dict[str, Any]:
        with self._lock:
            session = self.require(session_id)
            order = session.get("order")
            if not order:
                raise ValueError("order not found")
            protection = session.get("pickup_protection") or {}
            suggested = pickup_at or protection.get("suggested_pickup_at")
            if protection.get("status") != "SUGGESTED" or not suggested:
                raise ValueError("no pickup reschedule is waiting for acceptance")

            previous = order["pickup_at"]
            event = self._append_event_locked(
                session_id,
                "PickupRescheduled",
                {
                    "from_pickup_at": previous,
                    "pickup_at": suggested,
                    "capacity_units": order["units"],
                },
                detail=f"픽업 시간 변경 {previous} → {suggested}",
            )
            order["pickup_at"] = suggested
            order["status"] = "CONFIRMED"
            session["capacity"]["slot"] = suggested
            session["capacity"]["available_units"] = max(
                session["capacity"]["available_units"],
                order["units"],
            )
            session["pickup_protection"] = {
                "status": "RESCHEDULED",
                "original_pickup_at": protection.get("original_pickup_at") or previous,
                "suggested_pickup_at": suggested,
            }
            merchant = session.get("merchant_fulfillment") or _empty_merchant_fulfillment()
            if merchant.get("status") in {"RECEIVED", "ACCEPTED"}:
                merchant["window"] = _merchant_window(suggested, order["units"])
            elif merchant.get("status") in {"PREPARING", "READY"}:
                if "RESCHEDULE_AFTER_PREPARATION" not in merchant["anomalies"]:
                    merchant["anomalies"].append("RESCHEDULE_AFTER_PREPARATION")
            session["merchant_fulfillment"] = merchant
            pact = session["pickup_pact"]
            next_version = max(1, int(pact.get("version", 0))) + 1
            latest_at = _shift_hhmm(suggested, 3)
            session["pickup_pact"] = {
                "status": "ACTIVE",
                "promised_at": suggested,
                "latest_at": latest_at,
                "compensation_points": int(pact.get("compensation_points") or 500),
                "compensation_granted": False,
                "version": next_version,
            }
            self._append_event_locked(
                session_id,
                "PickupPactRenegotiated",
                {
                    "promised_at": suggested,
                    "latest_at": latest_at,
                    "compensation_points": session["pickup_pact"]["compensation_points"],
                    "version": next_version,
                },
                detail=f"새 보장 {suggested}~{latest_at}",
            )
            self._audit_locked(
                session_id,
                "PICKUP_PACT_RENEGOTIATED",
                f"v{next_version} · {suggested}~{latest_at}",
            )
            self._audit_locked(
                session_id,
                "PICKUP_RESCHEDULE_ACCEPTED",
                f"{previous} → {suggested}",
            )
            return {"event": deepcopy(event), "state": self.snapshot(session_id)}

    def merchant_snapshot(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            session = self.require(session_id)
            return deepcopy(session.get("merchant_fulfillment") or _empty_merchant_fulfillment())

    def merchant_ack(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            session = self.require(session_id)
            merchant = session.get("merchant_fulfillment") or _empty_merchant_fulfillment()
            if merchant.get("status") == "NONE":
                raise ValueError("merchant order not found")
            merchant["delivery_acknowledged"] = True
            session["merchant_fulfillment"] = merchant
            self._audit_locked(session_id, "MERCHANT_DELIVERY_ACK", "점주 앱이 주문 delivery를 ACK 했습니다.")
            return self.snapshot(session_id)

    def merchant_redeliver(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            session = self.require(session_id)
            merchant = session.get("merchant_fulfillment") or _empty_merchant_fulfillment()
            if merchant.get("status") == "NONE":
                raise ValueError("merchant order not found")
            merchant["redelivery_count"] = int(merchant.get("redelivery_count") or 0) + 1
            session["merchant_fulfillment"] = merchant
            self._audit_locked(
                session_id,
                "MERCHANT_DELIVERY_REDELIVERED",
                "동일 주문을 다시 전달했지만 POS/알림 effect는 추가 생성하지 않았습니다.",
            )
            return self.snapshot(session_id)

    def merchant_accept(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            session = self.require(session_id)
            merchant = session.get("merchant_fulfillment") or _empty_merchant_fulfillment()
            if merchant.get("status") == "ACCEPTED":
                return self.snapshot(session_id)
            if merchant.get("status") != "RECEIVED":
                raise ValueError("merchant order can be accepted only from RECEIVED")
            merchant["status"] = "ACCEPTED"
            session["merchant_fulfillment"] = merchant
            self._audit_locked(session_id, "MERCHANT_ORDER_ACCEPTED", "점주가 주문을 접수했습니다.")
            return self.snapshot(session_id)

    def merchant_start(self, session_id: str, timing: str = "ON_TIME") -> dict[str, Any]:
        with self._lock:
            session = self.require(session_id)
            merchant = session.get("merchant_fulfillment") or _empty_merchant_fulfillment()
            if merchant.get("status") != "ACCEPTED":
                raise ValueError("preparation can start only from ACCEPTED")
            timing = timing.upper()
            if timing == "TOO_EARLY":
                raise ValueError("preparation window has not opened")
            if timing not in {"ON_TIME", "LATE"}:
                raise ValueError("unknown preparation timing")
            merchant["status"] = "PREPARING"
            if timing == "LATE" and "STARTED_LATE" not in merchant["anomalies"]:
                merchant["anomalies"].append("STARTED_LATE")
            session["merchant_fulfillment"] = merchant
            self._audit_locked(
                session_id,
                "MERCHANT_PREPARATION_STARTED",
                f"제조 시작 · timing={timing}",
            )
            return self.snapshot(session_id)

    def merchant_ready(self, session_id: str, timing: str = "ON_TIME") -> dict[str, Any]:
        with self._lock:
            session = self.require(session_id)
            merchant = session.get("merchant_fulfillment") or _empty_merchant_fulfillment()
            if merchant.get("status") != "PREPARING":
                raise ValueError("order can become READY only from PREPARING")
            timing = timing.upper()
            if timing not in {"EARLY", "ON_TIME", "LATE"}:
                raise ValueError("unknown ready timing")

            merchant["status"] = "READY"
            merchant["ready_quality"] = timing
            anomaly = None
            if timing == "EARLY":
                anomaly = "READY_TOO_EARLY"
            elif timing == "LATE":
                anomaly = "READY_LATE"
            if anomaly and anomaly not in merchant["anomalies"]:
                merchant["anomalies"].append(anomaly)
            session["merchant_fulfillment"] = merchant
            self._audit_locked(
                session_id,
                "MERCHANT_ORDER_READY",
                f"조리 완료 · quality={timing}",
            )

            if timing == "LATE" and session["pickup_pact"].get("status") == "ACTIVE":
                self.breach_pickup_pact(session_id)

            return self.snapshot(session_id)

    def seed_from_scenario(
        self,
        session_id: str,
        scenario_id: str,
        scenario: dict[str, Any],
    ) -> dict[str, Any]:
        with self._lock:
            self.reset(session_id)
            session = self.require(session_id)
            order_meta = scenario["order"]
            amount = 0
            for item in scenario["events"]:
                if item["event_type"] == "PaymentAuthorized":
                    amount = _money(item["payload"].get("amount"))
                    break
            amount = amount or _money(order_meta.get("total"))

            units = 1
            for item in scenario["events"]:
                if "capacity_units" in item["payload"]:
                    units = int(item["payload"]["capacity_units"])
                    break

            session["order"] = {
                "order_id": order_meta["order_id"],
                "aggregate_id": scenario["events"][0]["aggregate_id"],
                "store": order_meta["store"],
                "items": order_meta["items"],
                "total": amount,
                "pickup_at": order_meta["pickup_at"],
                "units": units,
                "status": "CONFIRMED",
                "payment_authorized": True,
                "settled": False,
                "rewarded": False,
                "pickup_code": _new_pickup_code(),
                "pickup_claimed": False,
            }
            session["pickup_protection"] = {
                "status": "ON_TIME",
                "original_pickup_at": order_meta["pickup_at"],
                "suggested_pickup_at": None,
            }
            session["events"] = deepcopy(scenario["events"])
            session["capacity"]["slot"] = order_meta["pickup_at"]
            session["capacity"]["reserved_units"] = units

            financial_side_effects: set[str] = set()
            for event in session["events"]:
                if event["event_type"] == "CommitmentCancelled":
                    session["order"]["status"] = "CANCELLED"
                    session["capacity"]["reserved_units"] = 0
                elif event["event_type"] == "SettlementPosted":
                    session["order"]["settled"] = True
                    if event["event_id"] not in financial_side_effects:
                        financial_side_effects.add(event["event_id"])
                        self._ledger_locked(
                            session_id,
                            "SETTLEMENT",
                            _money(event["payload"].get("amount")),
                            event["event_id"],
                        )
                elif event["event_type"] == "RewardGranted":
                    session["order"]["rewarded"] = True
                    if event["event_id"] not in financial_side_effects:
                        financial_side_effects.add(event["event_id"])
                        self._ledger_locked(
                            session_id,
                            "REWARD",
                            _money(event["payload"].get("amount")),
                            event["event_id"],
                        )
                elif event["event_type"] == "CapacityRevised":
                    session["capacity"]["revision"] = int(event["payload"].get("revision", 2))
                    session["capacity"]["available_units"] = int(
                        event["payload"].get("available_units", session["capacity"]["available_units"])
                    )

            self._audit_locked(
                session_id,
                "PRESET_LOADED",
                f"{scenario_id} · {scenario['title']}",
            )
            return self.snapshot(session_id)

    def snapshot(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            session = self.require(session_id)
            reconciliation = self._reconcile_locked(session_id)
            events = deepcopy(session["events"])
            received_order = sorted(
                events,
                key=lambda event: (event["received_at"], event["event_id"]),
            )
            business_order = sorted(
                events,
                key=lambda event: (event["occurred_at"], event["event_id"]),
            )
            total_settlement = sum(
                batch["amount"]
                if batch["posting_type"] == "SETTLEMENT"
                else -batch["amount"]
                if batch["posting_type"] == "REVERSE_SETTLEMENT"
                else 0
                for batch in session["ledger_batches"]
            )
            reward_balance = sum(
                batch["amount"]
                if batch["posting_type"] == "REWARD"
                else -batch["amount"]
                if batch["posting_type"] == "REVERSE_REWARD"
                else 0
                for batch in session["ledger_batches"]
            )
            return {
                "session_id": session_id,
                "created_at": _iso(session["created_at"]),
                "updated_at": _iso(session["updated_at"]),
                "order": deepcopy(session["order"]),
                "capacity": deepcopy(session["capacity"]),
                "events": events,
                "received_order": received_order,
                "business_order": business_order,
                "ledger_batches": deepcopy(session["ledger_batches"]),
                "audit": list(reversed(deepcopy(session["audit"]))),
                "applied_repairs": list(session["applied_repairs"]),
                "manual_review": session["manual_review"],
                "projection_rebuilds": session["projection_rebuilds"],
                "pickup_protection": deepcopy(session["pickup_protection"]),
                "pickup_pact": deepcopy(session["pickup_pact"]),
                "merchant_fulfillment": deepcopy(
                    session.get("merchant_fulfillment") or _empty_merchant_fulfillment()
                ),
                "customer_history": deepcopy(session["customer_history"]),
                "reconciliation": reconciliation,
                "metrics": {
                    "event_count": len(events),
                    "anomaly_count": len(reconciliation["anomalies"]),
                    "repair_count": len(reconciliation["repairs"]),
                    "ledger_batch_count": len(session["ledger_batches"]),
                    "net_settlement": total_settlement,
                    "reward_balance": reward_balance,
                    "capacity_used": session["capacity"]["reserved_units"],
                    "capacity_available": session["capacity"]["available_units"],
                },
            }


demo_store = DemoStore()
