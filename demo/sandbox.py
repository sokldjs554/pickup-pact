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
            "currency": "KRW",
            "debit_account": debit,
            "credit_account": credit,
            "created_at": _iso(_now()),
        }
        session["ledger_batches"].append(batch)
        self._touch(session)
        return batch

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
            }
            session["capacity"]["slot"] = pickup_at
            session["capacity"]["reserved_units"] += units
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
            event = self._append_event_locked(
                session_id,
                "CommitmentConfirmed",
                {"capacity_units": order["units"], "pickup_at": order["pickup_at"]},
                detail=f"{order['pickup_at']} 픽업 약속 확정",
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
            after = self._reconcile_locked(session_id)
            return {
                "applied": applied,
                "reconciliation": deepcopy(after),
                "state": self.snapshot(session_id),
            }

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
