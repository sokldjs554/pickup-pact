#!/usr/bin/env python3
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx


COMMITMENT = "http://127.0.0.1:8080"
LEDGER = "http://127.0.0.1:8081"
MERCHANT = "http://127.0.0.1:8082"
RECONCILER = "http://127.0.0.1:8000"
OPS = "http://127.0.0.1:5000"


def postgres_connection():
    import psycopg
    return psycopg.connect(
        "postgresql://pickuppact:pickuppact@127.0.0.1:5432/pickuppact"
    )


def wait_until(predicate, *, timeout_s: int = 30, interval_s: float = 0.5, label: str) -> None:
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        try:
            last = predicate()
            if last:
                return
        except Exception as exc:
            last = repr(exc)
        time.sleep(interval_s)
    raise AssertionError(f"timed out waiting for {label}: last={last!r}")


def wait_http(url: str, *, timeout_s: int = 180) -> None:
    deadline = time.time() + timeout_s
    last: str | None = None
    while time.time() < deadline:
        try:
            response = httpx.get(url, timeout=5)
            if response.status_code == 200:
                return
            last = f"HTTP {response.status_code}: {response.text[:200]}"
        except Exception as exc:
            last = repr(exc)
        time.sleep(2)
    raise RuntimeError(f"timed out waiting for {url}: {last}")


def expect(response: httpx.Response, status: int | set[int]) -> dict:
    allowed = {status} if isinstance(status, int) else status
    if response.status_code not in allowed:
        raise AssertionError(
            f"{response.request.method} {response.request.url} "
            f"expected {sorted(allowed)}, got {response.status_code}: {response.text}"
        )
    if not response.content:
        return {}
    return response.json()


def commitment_flow(pass_no: int) -> None:
    store_id = f"integration-store-{pass_no}"
    quote = expect(
        httpx.post(
            f"{COMMITMENT}/api/v1/commitments/quotes",
            json={
                "storeId": store_id,
                "items": [{"sku": "cafe-latte", "quantity": 1}],
                "count": 3,
            },
            timeout=15,
        ),
        200,
    )
    assert quote["storeId"] == store_id, quote
    assert quote["units"] == 2, quote
    assert quote["totalAmount"] == 5000, quote
    slot_options = quote["slots"]
    assert len(slot_options) == 3, slot_options
    assert all(option["pickupAt"] for option in slot_options), slot_options
    assert all(option["canFit"] is True for option in slot_options), slot_options
    assert slot_options[0]["capacityUnits"] == 40, slot_options
    assert slot_options[0]["reservedUnits"] == 0, slot_options
    assert slot_options[0]["availableUnits"] == 40, slot_options

    pickup_at = slot_options[0]["pickupAt"]
    idempotency_key = f"integration-hold-{pass_no}-{uuid.uuid4().hex[:8]}"
    hold_payload = {
        "quoteToken": quote["quoteToken"],
        "pickupAt": pickup_at,
    }
    held = expect(
        httpx.post(
            f"{COMMITMENT}/api/v1/commitments/hold",
            headers={"Idempotency-Key": idempotency_key},
            json=hold_payload,
            timeout=15,
        ),
        200,
    )
    retry_hold = expect(
        httpx.post(
            f"{COMMITMENT}/api/v1/commitments/hold",
            headers={"Idempotency-Key": idempotency_key},
            json=hold_payload,
            timeout=15,
        ),
        200,
    )
    assert retry_hold["id"] == held["id"], (held, retry_hold)
    assert held["state"] == "HELD", held
    assert held["paymentAuthorized"] is False, held
    assert held["units"] == 2, held
    commitment_id = held["id"]

    token_parts = held["leaseToken"].split("|")
    assert len(token_parts) == 2, held
    slot_key, lease_key = token_parts

    import redis
    redis_client = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
    assert redis_client.get(slot_key) == "2", held
    assert redis_client.exists(lease_key) == 1, held

    missing_payment = httpx.post(
        f"{COMMITMENT}/api/v1/commitments/{commitment_id}/confirm",
        timeout=15,
    )
    assert missing_payment.status_code == 409, missing_payment.text

    reserved = expect(
        httpx.get(
            f"{COMMITMENT}/api/v1/commitments/slots",
            params={
                "storeId": store_id,
                "from": pickup_at,
                "count": 1,
                "units": 2,
            },
            timeout=15,
        ),
        200,
    )
    assert reserved[0]["pickupAt"] == pickup_at, reserved
    assert reserved[0]["reservedUnits"] == 2, reserved
    assert reserved[0]["availableUnits"] == 38, reserved
    assert reserved[0]["canFit"] is True, reserved

    paid = expect(
        httpx.post(
            f"{COMMITMENT}/api/v1/commitments/{commitment_id}/authorize-payment",
            json={"authorizationId": f"auth-integration-{pass_no}"},
            timeout=15,
        ),
        200,
    )
    assert paid["paymentAuthorized"] is True, paid

    duplicate_payment = httpx.post(
        f"{COMMITMENT}/api/v1/commitments/{commitment_id}/authorize-payment",
        json={"authorizationId": f"auth-integration-{pass_no}"},
        timeout=15,
    )
    assert duplicate_payment.status_code == 409, duplicate_payment.text

    confirmed = expect(
        httpx.post(
            f"{COMMITMENT}/api/v1/commitments/{commitment_id}/confirm",
            timeout=15,
        ),
        200,
    )
    assert confirmed["state"] == "CONFIRMED", confirmed
    assert confirmed["pact"]["status"] == "ACTIVE", confirmed
    assert confirmed["pact"]["version"] == 1, confirmed
    assert confirmed["pact"]["compensationPoints"] == 500, confirmed

    tracked = expect(
        httpx.get(
            f"{COMMITMENT}/api/v1/commitments/{commitment_id}",
            timeout=15,
        ),
        200,
    )
    assert tracked["id"] == commitment_id, tracked
    assert tracked["state"] == "CONFIRMED", tracked
    assert tracked["pact"]["status"] == "ACTIVE", tracked

    cancellation_requested = expect(
        httpx.post(
            f"{COMMITMENT}/api/v1/commitments/{commitment_id}/cancel",
            timeout=15,
        ),
        200,
    )
    assert cancellation_requested["state"] == "CONFIRMED", cancellation_requested
    assert cancellation_requested["cancellationRequestId"], cancellation_requested
    assert redis_client.exists(slot_key) == 1, cancellation_requested

    def merchant_approved_cancellation():
        merchant = httpx.get(
            f"{MERCHANT}/api/v1/merchant/orders/{commitment_id}",
            timeout=10,
        )
        commitment = httpx.get(
            f"{COMMITMENT}/api/v1/commitments/{commitment_id}",
            timeout=10,
        )
        return (
            merchant.status_code == 200
            and merchant.json()["state"] == "CANCELLED"
            and commitment.status_code == 200
            and commitment.json()["state"] == "CANCELLED"
        )

    wait_until(
        merchant_approved_cancellation,
        timeout_s=30,
        label="merchant-authorized confirmed cancellation",
    )
    cancelled = expect(
        httpx.get(f"{COMMITMENT}/api/v1/commitments/{commitment_id}", timeout=15),
        200,
    )
    assert cancelled["pact"]["status"] == "CANCELLED", cancelled
    assert cancelled["cancellationRequestId"] is None, cancelled
    assert redis_client.exists(slot_key) == 0, cancelled
    assert redis_client.exists(lease_key) == 0, cancelled

    retry = expect(
        httpx.post(
            f"{COMMITMENT}/api/v1/commitments/{commitment_id}/cancel",
            timeout=15,
        ),
        200,
    )
    assert retry["state"] == "CANCELLED", retry
    assert redis_client.exists(slot_key) == 0, retry
    assert redis_client.exists(lease_key) == 0, retry

    released = expect(
        httpx.get(
            f"{COMMITMENT}/api/v1/commitments/slots",
            params={
                "storeId": store_id,
                "from": pickup_at,
                "count": 1,
                "units": 2,
            },
            timeout=15,
        ),
        200,
    )
    assert released[0]["reservedUnits"] == 0, released
    assert released[0]["availableUnits"] == 40, released

    def outbox_published():
        with postgres_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    select count(*) filter (where published_at is not null), count(*)
                    from outbox_events
                    where aggregate_id = %s::uuid
                    """,
                    (commitment_id,),
                )
                published, total = cursor.fetchone()
                return total == 6 and published == total

    wait_until(
        outbox_published,
        timeout_s=30,
        label="commitment and Pact outbox delivery to Kafka",
    )

    with postgres_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                select event_type
                from outbox_events
                where aggregate_id = %s::uuid
                order by event_sequence
                """,
                (commitment_id,),
            )
            ordered_types = [row[0] for row in cursor.fetchall()]
    assert ordered_types == [
        "PickupSlotHeld",
        "PaymentAuthorized",
        "CommitmentConfirmed",
        "PickupPactIssued",
        "CancellationRequested",
        "CommitmentCancelled",
    ], ordered_types


def capacity_concurrency_flow(pass_no: int) -> None:
    store_id = f"concurrency-store-{pass_no}"
    quote = expect(
        httpx.post(
            f"{COMMITMENT}/api/v1/commitments/quotes",
            json={
                "storeId": store_id,
                "items": [{"sku": "cafe-latte", "quantity": 1}],
                "count": 1,
            },
            timeout=15,
        ),
        200,
    )
    assert quote["units"] == 2, quote
    pickup_at = quote["slots"][0]["pickupAt"]
    quote_token = quote["quoteToken"]

    def hold_one(index: int) -> httpx.Response:
        return httpx.post(
            f"{COMMITMENT}/api/v1/commitments/hold",
            headers={"Idempotency-Key": f"capacity-{pass_no}-{index}-{uuid.uuid4().hex[:8]}"},
            json={
                "quoteToken": quote_token,
                "pickupAt": pickup_at,
            },
            timeout=20,
        )

    with ThreadPoolExecutor(max_workers=30) as pool:
        responses = list(pool.map(hold_one, range(30)))

    statuses = [response.status_code for response in responses]
    assert set(statuses) <= {200, 409}, statuses
    assert statuses.count(200) == 20, statuses
    assert statuses.count(409) == 10, statuses

    accepted = [response.json() for response in responses if response.status_code == 200]
    live_slot = expect(
        httpx.get(
            f"{COMMITMENT}/api/v1/commitments/slots",
            params={
                "storeId": store_id,
                "from": pickup_at,
                "count": 1,
                "units": 1,
            },
            timeout=15,
        ),
        200,
    )[0]
    assert live_slot["reservedUnits"] == 40, live_slot
    assert live_slot["availableUnits"] == 0, live_slot
    assert live_slot["canFit"] is False, live_slot

    for commitment in accepted:
        cancelled = expect(
            httpx.post(
                f"{COMMITMENT}/api/v1/commitments/{commitment['id']}/cancel",
                timeout=15,
            ),
            200,
        )
        assert cancelled["state"] == "CANCELLED", cancelled

    released_slot = expect(
        httpx.get(
            f"{COMMITMENT}/api/v1/commitments/slots",
            params={
                "storeId": store_id,
                "from": pickup_at,
                "count": 1,
                "units": 1,
            },
            timeout=15,
        ),
        200,
    )[0]
    assert released_slot["reservedUnits"] == 0, released_slot
    assert released_slot["availableUnits"] == 40, released_slot



def idempotency_concurrency_flow(pass_no: int) -> None:
    store_id = f"idempotency-store-{pass_no}"
    quote = expect(
        httpx.post(
            f"{COMMITMENT}/api/v1/commitments/quotes",
            json={
                "storeId": store_id,
                "items": [{"sku": "cafe-latte", "quantity": 1}],
                "count": 1,
            },
            timeout=15,
        ),
        200,
    )
    pickup_at = quote["slots"][0]["pickupAt"]
    idempotency_key = f"same-key-{pass_no}-{uuid.uuid4().hex[:8]}"

    def retry_same_hold(_: int) -> httpx.Response:
        return httpx.post(
            f"{COMMITMENT}/api/v1/commitments/hold",
            headers={"Idempotency-Key": idempotency_key},
            json={"quoteToken": quote["quoteToken"], "pickupAt": pickup_at},
            timeout=20,
        )

    with ThreadPoolExecutor(max_workers=10) as pool:
        responses = list(pool.map(retry_same_hold, range(10)))

    assert all(response.status_code == 200 for response in responses), [
        (response.status_code, response.text) for response in responses
    ]
    commitment_ids = {response.json()["id"] for response in responses}
    assert len(commitment_ids) == 1, commitment_ids

    live_slot = expect(
        httpx.get(
            f"{COMMITMENT}/api/v1/commitments/slots",
            params={"storeId": store_id, "from": pickup_at, "count": 1, "units": 1},
            timeout=15,
        ),
        200,
    )[0]
    assert live_slot["reservedUnits"] == 2, live_slot

    commitment_id = next(iter(commitment_ids))
    expect(
        httpx.post(
            f"{COMMITMENT}/api/v1/commitments/{commitment_id}/cancel",
            timeout=15,
        ),
        200,
    )



def pact_financial_flow(pass_no: int) -> None:
    store_id = f"pact-financial-store-{pass_no}"
    quote = expect(
        httpx.post(
            f"{COMMITMENT}/api/v1/commitments/quotes",
            json={
                "storeId": store_id,
                "items": [{"sku": "americano", "quantity": 1}],
                "count": 1,
            },
            timeout=15,
        ),
        200,
    )
    pickup_at = quote["slots"][0]["pickupAt"]
    held = expect(
        httpx.post(
            f"{COMMITMENT}/api/v1/commitments/hold",
            headers={"Idempotency-Key": f"pact-financial-{pass_no}-{uuid.uuid4().hex[:8]}"},
            json={"quoteToken": quote["quoteToken"], "pickupAt": pickup_at},
            timeout=15,
        ),
        200,
    )
    commitment_id = held["id"]
    expect(
        httpx.post(
            f"{COMMITMENT}/api/v1/commitments/{commitment_id}/authorize-payment",
            json={"authorizationId": f"auth-pact-{pass_no}"},
            timeout=15,
        ),
        200,
    )
    confirmed = expect(
        httpx.post(f"{COMMITMENT}/api/v1/commitments/{commitment_id}/confirm", timeout=15),
        200,
    )
    assert confirmed["pact"]["status"] == "ACTIVE", confirmed

    early_breach = httpx.post(
        f"{COMMITMENT}/api/v1/commitments/{commitment_id}/breach-pact",
        timeout=15,
    )
    assert early_breach.status_code == 409, early_breach.text

    with postgres_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                update pickup_commitments
                set pact_promised_at = now() - interval '10 minutes',
                    pact_latest_at = now() - interval '5 minutes'
                where id = %s::uuid
                """,
                (commitment_id,),
            )
        connection.commit()

    breached = expect(
        httpx.post(f"{COMMITMENT}/api/v1/commitments/{commitment_id}/breach-pact", timeout=15),
        200,
    )
    assert breached["pact"]["status"] == "COMPENSATED", breached
    assert breached["pact"]["compensationGranted"] is True, breached

    duplicate_breach = httpx.post(
        f"{COMMITMENT}/api/v1/commitments/{commitment_id}/breach-pact",
        timeout=15,
    )
    assert duplicate_breach.status_code == 409, duplicate_breach.text

    def compensation_reaches_ledger():
        response = httpx.get(
            f"{LEDGER}/api/v1/ledger/orders/{commitment_id}?limit=10",
            timeout=10,
        )
        return (
            response.status_code == 200
            and len(response.json()) == 1
            and response.json()[0]["reason"] == "REWARD"
            and response.json()[0]["amount"] == 500
            and response.json()[0]["unit"] == "PTS"
            and response.json()[0]["eventId"].endswith("-pact-reward")
        )

    wait_until(
        compensation_reaches_ledger,
        timeout_s=30,
        label="Pickup Pact breach compensation through outbox Kafka ledger",
    )

    claimed = expect(
        httpx.post(f"{COMMITMENT}/api/v1/commitments/{commitment_id}/claim-pickup", timeout=15),
        200,
    )
    assert claimed["state"] == "PICKED_UP", claimed
    assert claimed["pact"]["status"] == "COMPENSATED", claimed

    def settlement_reaches_ledger():
        response = httpx.get(
            f"{LEDGER}/api/v1/ledger/orders/{commitment_id}?limit=10",
            timeout=10,
        )
        if response.status_code != 200:
            return False
        rows = response.json()
        reasons = sorted(item["reason"] for item in rows)
        return (
            len(rows) == 2
            and reasons == ["REWARD", "SETTLEMENT"]
            and any(
                item["eventId"].endswith("-settlement")
                and item["amount"] == 4500
                and item["unit"] == "KRW"
                for item in rows
            )
            and any(
                item["eventId"].endswith("-pact-reward")
                and item["amount"] == 500
                and item["unit"] == "PTS"
                for item in rows
            )
        )

    wait_until(
        settlement_reaches_ledger,
        timeout_s=30,
        label="PickupClaimed settlement through outbox Kafka ledger",
    )


def merchant_fulfillment_flow(pass_no: int) -> None:
    store_id = f"merchant-store-{pass_no}"

    def confirmed_order(suffix: str, sku: str = "cafe-latte") -> tuple[str, str]:
        quote = expect(
            httpx.post(
                f"{COMMITMENT}/api/v1/commitments/quotes",
                json={
                    "storeId": store_id,
                    "items": [{"sku": sku, "quantity": 1}],
                    "count": 1,
                },
                timeout=15,
            ),
            200,
        )
        pickup_at = quote["slots"][0]["pickupAt"]
        held = expect(
            httpx.post(
                f"{COMMITMENT}/api/v1/commitments/hold",
                headers={"Idempotency-Key": f"merchant-{pass_no}-{suffix}-{uuid.uuid4().hex[:8]}"},
                json={"quoteToken": quote["quoteToken"], "pickupAt": pickup_at},
                timeout=15,
            ),
            200,
        )
        order_id = held["id"]
        expect(
            httpx.post(
                f"{COMMITMENT}/api/v1/commitments/{order_id}/authorize-payment",
                json={"authorizationId": f"merchant-auth-{pass_no}-{suffix}"},
                timeout=15,
            ),
            200,
        )
        expect(
            httpx.post(
                f"{COMMITMENT}/api/v1/commitments/{order_id}/confirm",
                timeout=15,
            ),
            200,
        )
        return order_id, pickup_at

    order_id, pickup_at = confirmed_order("reconnect")

    def merchant_order_received():
        response = httpx.get(f"{MERCHANT}/api/v1/merchant/orders/{order_id}", timeout=10)
        return response.status_code == 200 and response.json()["state"] == "RECEIVED"

    wait_until(merchant_order_received, timeout_s=30, label="merchant confirmed-order intake")

    deliveries = expect(
        httpx.get(
            f"{MERCHANT}/api/v1/merchant/stores/{store_id}/deliveries",
            params={"afterSequence": 0, "limit": 20},
            timeout=15,
        ),
        200,
    )
    order_deliveries = [item for item in deliveries if item["orderId"] == order_id]
    assert len(order_deliveries) == 1, deliveries
    assert order_deliveries[0]["type"] == "ORDER_AVAILABLE", order_deliveries
    delivery_sequence = order_deliveries[0]["sequence"]

    effects = expect(
        httpx.get(f"{MERCHANT}/api/v1/merchant/orders/{order_id}/effects", timeout=15),
        200,
    )
    assert sorted(item["type"] for item in effects) == [
        "NEW_ORDER_NOTIFICATION",
        "POS_PRINT",
    ], effects

    # Simulate Kafka at-least-once redelivery using the exact same event ID.
    with postgres_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                select id::text, payload::text, occurred_at
                from outbox_events
                where aggregate_id=%s::uuid and event_type='CommitmentConfirmed'
                order by event_sequence
                limit 1
                """,
                (order_id,),
            )
            event_id, raw_payload, occurred_at = cursor.fetchone()
    duplicate_envelope = {
        "event_id": event_id,
        "aggregate_id": order_id,
        "event_type": "CommitmentConfirmed",
        "occurred_at": occurred_at.isoformat(),
        "schema_version": 1,
        "payload": json.loads(raw_payload),
    }
    subprocess.run(
        [
            "docker", "compose", "exec", "-T", "kafka",
            "/opt/kafka/bin/kafka-console-producer.sh",
            "--bootstrap-server", "localhost:9092",
            "--topic", "pickup.commitment.events.v1",
            "--property", f"parse.key=true",
            "--property", "key.separator=|",
        ],
        input=f"{order_id}|{json.dumps(duplicate_envelope)}\n",
        text=True,
        check=True,
        timeout=30,
    )
    time.sleep(2)

    effects_after_redelivery = expect(
        httpx.get(f"{MERCHANT}/api/v1/merchant/orders/{order_id}/effects", timeout=15),
        200,
    )
    assert len(effects_after_redelivery) == 2, effects_after_redelivery

    reconnect_deliveries = expect(
        httpx.get(
            f"{MERCHANT}/api/v1/merchant/stores/{store_id}/deliveries",
            params={"afterSequence": 0, "limit": 20},
            timeout=15,
        ),
        200,
    )
    assert any(item["sequence"] == delivery_sequence for item in reconnect_deliveries), reconnect_deliveries

    expect(
        httpx.post(
            f"{MERCHANT}/api/v1/merchant/deliveries/{delivery_sequence}/ack",
            timeout=15,
        ),
        200,
    )
    after_ack = expect(
        httpx.get(
            f"{MERCHANT}/api/v1/merchant/stores/{store_id}/deliveries",
            params={"afterSequence": 0, "limit": 20},
            timeout=15,
        ),
        200,
    )
    assert not any(item["sequence"] == delivery_sequence for item in after_ack), after_ack

    accepted = expect(
        httpx.post(f"{MERCHANT}/api/v1/merchant/orders/{order_id}/accept", timeout=15),
        200,
    )
    assert accepted["state"] == "ACCEPTED", accepted

    # Deterministically prove the too-early start guard, then an EARLY ready anomaly.
    with postgres_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                update merchant_orders
                set earliest_start_at=now()+interval '10 minutes',
                    target_ready_at=now()+interval '15 minutes',
                    latest_ready_at=now()+interval '20 minutes'
                where order_id=%s::uuid
                """,
                (order_id,),
            )
        connection.commit()

    too_early = httpx.post(
        f"{MERCHANT}/api/v1/merchant/orders/{order_id}/start",
        timeout=15,
    )
    assert too_early.status_code == 409, too_early.text

    with postgres_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                update merchant_orders
                set earliest_start_at=now()-interval '1 minute',
                    target_ready_at=now()+interval '5 minutes',
                    latest_ready_at=now()+interval '10 minutes'
                where order_id=%s::uuid
                """,
                (order_id,),
            )
        connection.commit()

    preparing = expect(
        httpx.post(f"{MERCHANT}/api/v1/merchant/orders/{order_id}/start", timeout=15),
        200,
    )
    assert preparing["state"] == "PREPARING", preparing
    early_ready = expect(
        httpx.post(f"{MERCHANT}/api/v1/merchant/orders/{order_id}/ready", timeout=15),
        200,
    )
    assert early_ready["state"] == "READY", early_ready

    anomalies = expect(
        httpx.get(f"{MERCHANT}/api/v1/merchant/orders/{order_id}/anomalies", timeout=15),
        200,
    )
    assert any(item["code"] == "READY_TOO_EARLY" for item in anomalies), anomalies

    new_pickup_at = (
        datetime.fromisoformat(pickup_at.replace("Z", "+00:00")) + timedelta(minutes=10)
    ).isoformat()
    expect(
        httpx.post(
            f"{COMMITMENT}/api/v1/commitments/{order_id}/reschedule",
            json={"pickupAt": new_pickup_at},
            timeout=15,
        ),
        200,
    )

    def schedule_review_visible():
        rows = httpx.get(
            f"{MERCHANT}/api/v1/merchant/orders/{order_id}/anomalies",
            timeout=10,
        )
        return (
            rows.status_code == 200
            and any(item["code"] == "RESCHEDULE_AFTER_PREPARATION" for item in rows.json())
        )

    wait_until(schedule_review_visible, timeout_s=30, label="post-preparation reschedule review")

    pending_cancel = expect(
        httpx.post(f"{COMMITMENT}/api/v1/commitments/{order_id}/cancel", timeout=15),
        200,
    )
    assert pending_cancel["state"] == "CONFIRMED", pending_cancel
    assert pending_cancel["cancellationRequestId"], pending_cancel

    def cancellation_rejected_after_preparation():
        merchant = httpx.get(f"{MERCHANT}/api/v1/merchant/orders/{order_id}", timeout=10)
        commitment = httpx.get(f"{COMMITMENT}/api/v1/commitments/{order_id}", timeout=10)
        anomalies = httpx.get(
            f"{MERCHANT}/api/v1/merchant/orders/{order_id}/anomalies",
            timeout=10,
        )
        return (
            merchant.status_code == 200
            and merchant.json()["state"] == "READY"
            and commitment.status_code == 200
            and commitment.json()["state"] == "CONFIRMED"
            and commitment.json().get("cancellationRequestId") is None
            and anomalies.status_code == 200
            and any(item["code"] == "CANCEL_AFTER_PREPARATION" for item in anomalies.json())
        )

    wait_until(
        cancellation_rejected_after_preparation,
        timeout_s=30,
        label="merchant rejects cancellation after preparation",
    )

    # Second order proves READY_LATE -> fulfillment event -> Pickup Pact -> 500 PTS.
    late_order_id, _ = confirmed_order("late", sku="americano")

    wait_until(
        lambda: httpx.get(
            f"{MERCHANT}/api/v1/merchant/orders/{late_order_id}", timeout=10
        ).status_code == 200,
        timeout_s=30,
        label="late merchant order intake",
    )
    expect(
        httpx.post(f"{MERCHANT}/api/v1/merchant/orders/{late_order_id}/accept", timeout=15),
        200,
    )

    with postgres_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                update merchant_orders
                set earliest_start_at=now()-interval '10 minutes',
                    target_ready_at=now()-interval '5 minutes',
                    latest_ready_at=now()-interval '1 minute'
                where order_id=%s::uuid
                """,
                (late_order_id,),
            )
            cursor.execute(
                """
                update pickup_commitments
                set pact_promised_at=now()-interval '10 minutes',
                    pact_latest_at=now()-interval '1 minute'
                where id=%s::uuid
                """,
                (late_order_id,),
            )
        connection.commit()

    expect(
        httpx.post(f"{MERCHANT}/api/v1/merchant/orders/{late_order_id}/start", timeout=15),
        200,
    )
    expect(
        httpx.post(f"{MERCHANT}/api/v1/merchant/orders/{late_order_id}/ready", timeout=15),
        200,
    )

    def late_pact_compensated():
        response = httpx.get(
            f"{COMMITMENT}/api/v1/commitments/{late_order_id}",
            timeout=10,
        )
        return (
            response.status_code == 200
            and response.json().get("pact", {}).get("status") == "COMPENSATED"
        )

    wait_until(
        late_pact_compensated,
        timeout_s=30,
        label="READY_LATE automatic Pickup Pact compensation",
    )

    def late_reward_in_ledger():
        response = httpx.get(
            f"{LEDGER}/api/v1/ledger/orders/{late_order_id}?limit=10",
            timeout=10,
        )
        return (
            response.status_code == 200
            and any(
                item["reason"] == "REWARD"
                and item["amount"] == 500
                and item["unit"] == "PTS"
                for item in response.json()
            )
        )

    wait_until(
        late_reward_in_ledger,
        timeout_s=30,
        label="READY_LATE 500 PTS ledger posting",
    )


def ledger_flow(pass_no: int) -> None:
    event_id = f"integration-ledger-{pass_no}-{uuid.uuid4().hex[:8]}"
    payload = {
        "eventId": event_id,
        "aggregateId": f"integration-order-{pass_no}",
        "type": "SETTLEMENT",
        "amount": 12000,
    }

    first = expect(
        httpx.post(f"{LEDGER}/api/v1/ledger/postings", json=payload, timeout=15),
        202,
    )
    assert first["result"] == "POSTED", first

    duplicate = expect(
        httpx.post(f"{LEDGER}/api/v1/ledger/postings", json=payload, timeout=15),
        200,
    )
    assert duplicate["result"] == "DUPLICATE_NOOP", duplicate

    conflicting = expect(
        httpx.post(
            f"{LEDGER}/api/v1/ledger/postings",
            json={**payload, "amount": 13000},
            timeout=15,
        ),
        409,
    )
    assert conflicting["result"] == "CONFLICTING_EVENT_ID", conflicting

    history = expect(
        httpx.get(
            f"{LEDGER}/api/v1/ledger/orders/{payload['aggregateId']}?limit=10",
            timeout=15,
        ),
        200,
    )
    assert len(history) == 1, history
    assert history[0]["eventId"] == event_id, history
    assert history[0]["amount"] == 12000, history
    assert history[0]["unit"] == "KRW", history

    conflicts = expect(
        httpx.get(f"{LEDGER}/api/v1/ledger/conflicts?limit=20", timeout=15),
        200,
    )
    assert any(
        item["eventId"] == event_id
        and item["incomingFingerprint"] != item["existingFingerprint"]
        for item in conflicts
    ), conflicts

    allocation_order = f"integration-allocation-{pass_no}-{uuid.uuid4().hex[:8]}"
    first_source = f"{allocation_order}-settle-1"
    second_source = f"{allocation_order}-settle-2"

    for source_id, amount in ((first_source, 9000), (second_source, 2000)):
        expect(
            httpx.post(
                f"{LEDGER}/api/v1/ledger/postings",
                json={
                    "eventId": source_id,
                    "aggregateId": allocation_order,
                    "type": "SETTLEMENT",
                    "amount": amount,
                },
                timeout=15,
            ),
            202,
        )

    partial = expect(
        httpx.post(
            f"{LEDGER}/api/v1/ledger/postings",
            json={
                "eventId": f"{allocation_order}-reverse-3000",
                "aggregateId": allocation_order,
                "type": "REVERSE_SETTLEMENT",
                "amount": 3000,
                "sourceEventId": first_source,
            },
            timeout=15,
        ),
        202,
    )
    assert partial["result"] == "POSTED", partial

    too_much = expect(
        httpx.post(
            f"{LEDGER}/api/v1/ledger/postings",
            json={
                "eventId": f"{allocation_order}-reverse-too-much",
                "aggregateId": allocation_order,
                "type": "REVERSE_SETTLEMENT",
                "amount": 7000,
                "sourceEventId": first_source,
            },
            timeout=15,
        ),
        409,
    )
    assert too_much["result"] == "SOURCE_POSTING_CONFLICT", too_much

    expect(
        httpx.post(
            f"{LEDGER}/api/v1/ledger/postings",
            json={
                "eventId": f"{allocation_order}-reverse-rest",
                "aggregateId": allocation_order,
                "type": "REVERSE_SETTLEMENT",
                "amount": 6000,
                "sourceEventId": first_source,
            },
            timeout=15,
        ),
        202,
    )
    expect(
        httpx.post(
            f"{LEDGER}/api/v1/ledger/postings",
            json={
                "eventId": f"{allocation_order}-reverse-second",
                "aggregateId": allocation_order,
                "type": "REVERSE_SETTLEMENT",
                "amount": 2000,
                "sourceEventId": second_source,
            },
            timeout=15,
        ),
        202,
    )

    allocation_history = expect(
        httpx.get(
            f"{LEDGER}/api/v1/ledger/orders/{allocation_order}?limit=20",
            timeout=15,
        ),
        200,
    )
    reversals = [row for row in allocation_history if row["reason"] == "REVERSE_SETTLEMENT"]
    assert sorted((row["sourceEventId"], int(row["amount"])) for row in reversals) == [
        (first_source, 3000),
        (first_source, 6000),
        (second_source, 2000),
    ], allocation_history


def kafka_ledger_flow(pass_no: int) -> None:
    event_id = f"integration-kafka-ledger-{pass_no}-{uuid.uuid4().hex[:8]}"
    aggregate_id = f"integration-kafka-order-{pass_no}"

    def publish(amount: int) -> None:
        payload = {
            "eventId": event_id,
            "aggregateId": aggregate_id,
            "type": "REWARD",
            "amount": amount,
        }
        subprocess.run(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "kafka",
                "/opt/kafka/bin/kafka-console-producer.sh",
                "--bootstrap-server",
                "localhost:9092",
                "--topic",
                "pickup.financial.events.v1",
            ],
            input=json.dumps(payload) + "\n",
            text=True,
            check=True,
            timeout=30,
        )

    publish(120)

    def history_has_one():
        response = httpx.get(
            f"{LEDGER}/api/v1/ledger/orders/{aggregate_id}?limit=10",
            timeout=10,
        )
        return (
            response.status_code == 200
            and len(response.json()) == 1
            and response.json()[0]["eventId"] == event_id
        )

    wait_until(history_has_one, timeout_s=30, label="Kafka ledger consumer posting")

    publish(120)
    time.sleep(2)
    history = expect(
        httpx.get(f"{LEDGER}/api/v1/ledger/orders/{aggregate_id}?limit=10", timeout=10),
        200,
    )
    assert len(history) == 1, history

    publish(999)

    def conflict_is_quarantined():
        response = httpx.get(f"{LEDGER}/api/v1/ledger/conflicts?limit=50", timeout=10)
        return (
            response.status_code == 200
            and any(item["eventId"] == event_id for item in response.json())
        )

    wait_until(
        conflict_is_quarantined,
        timeout_s=30,
        label="Kafka conflicting event quarantine",
    )


def late_cancel_packet(pass_no: int) -> tuple[str, dict]:
    aggregate = f"integration-reconcile-{pass_no}-{uuid.uuid4().hex[:8]}"
    base = datetime.now(UTC)
    events = [
        {
            "event_id": f"{aggregate}-hold",
            "aggregate_id": aggregate,
            "event_type": "PickupSlotHeld",
            "occurred_at": (base + timedelta(seconds=0)).isoformat(),
            "received_at": (base + timedelta(seconds=0)).isoformat(),
            "payload": {"capacity_units": 2},
        },
        {
            "event_id": f"{aggregate}-pay",
            "aggregate_id": aggregate,
            "event_type": "PaymentAuthorized",
            "occurred_at": (base + timedelta(seconds=1)).isoformat(),
            "received_at": (base + timedelta(seconds=1)).isoformat(),
            "payload": {"amount": "12000"},
        },
        {
            "event_id": f"{aggregate}-confirm",
            "aggregate_id": aggregate,
            "event_type": "CommitmentConfirmed",
            "occurred_at": (base + timedelta(seconds=2)).isoformat(),
            "received_at": (base + timedelta(seconds=2)).isoformat(),
            "payload": {"capacity_units": 2},
        },
        {
            "event_id": f"{aggregate}-cancel",
            "aggregate_id": aggregate,
            "event_type": "CommitmentCancelled",
            "occurred_at": (base + timedelta(seconds=5)).isoformat(),
            "received_at": (base + timedelta(seconds=50)).isoformat(),
            "payload": {"reason": "integration"},
        },
        {
            "event_id": f"{aggregate}-settle",
            "aggregate_id": aggregate,
            "event_type": "SettlementPosted",
            "occurred_at": (base + timedelta(seconds=10)).isoformat(),
            "received_at": (base + timedelta(seconds=10)).isoformat(),
            "payload": {"amount": "12000"},
        },
        {
            "event_id": f"{aggregate}-reward",
            "aggregate_id": aggregate,
            "event_type": "RewardGranted",
            "occurred_at": (base + timedelta(seconds=11)).isoformat(),
            "received_at": (base + timedelta(seconds=11)).isoformat(),
            "payload": {"amount": "120"},
        },
    ]
    return aggregate, {"events": events}


def reconciler_flow(pass_no: int) -> None:
    aggregate, packet = late_cancel_packet(pass_no)

    result = expect(
        httpx.post(
            f"{RECONCILER}/api/v1/reconcile",
            json=packet,
            timeout=30,
        ),
        200,
    )
    assert "REVERSE_SETTLEMENT" in result["repairs"], result
    assert "REVERSE_REWARD" in result["repairs"], result
    assert "settlement_posted_after_prior_cancellation" in result["anomalies"], result

    def postgres_audit_written():
        with postgres_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "select count(*) from reconciliation_run where aggregate_id = %s",
                    (aggregate,),
                )
                return cursor.fetchone()[0] >= 1

    wait_until(
        postgres_audit_written,
        timeout_s=20,
        label="PostgreSQL reconciliation audit",
    )

    from pymongo import MongoClient
    mongo = MongoClient("mongodb://127.0.0.1:27017/pickup_pact", serverSelectionTimeoutMS=5000)
    wait_until(
        lambda: mongo.pickup_pact.raw_event_deliveries.count_documents(
            {"aggregate_id": aggregate}
        ) >= len(packet["events"]),
        timeout_s=20,
        label="MongoDB raw event deliveries",
    )

    # Persistence path: Elasticsearch indexing is near-real-time, so retry the query.
    deadline = time.time() + 30
    incidents: list[dict] = []
    while time.time() < deadline:
        response = httpx.get(
            f"{RECONCILER}/api/v1/incidents/{aggregate}",
            timeout=10,
        )
        if response.status_code == 200:
            incidents = response.json().get("incidents", [])
            if incidents:
                break
        time.sleep(1)
    assert incidents, f"no persisted Elasticsearch incident found for {aggregate}"

    replay = expect(
        httpx.post(
            f"{RECONCILER}/api/v1/replay",
            json=packet,
            timeout=20,
        ),
        200,
    )
    assert replay["ai_review"]["advisory_only"] is True, replay
    assert replay["ai_review"]["provider"] == "offline", replay

    rebuilt = expect(
        httpx.post(
            f"{RECONCILER}/api/v1/projections/rebuild",
            json=packet,
            timeout=20,
        ),
        200,
    )
    assert rebuilt["projection"]["status"] == "CANCELLED", rebuilt

    stored_projection = expect(
        httpx.get(
            f"{RECONCILER}/api/v1/projections/{aggregate}",
            timeout=15,
        ),
        200,
    )
    assert stored_projection["projection"]["status"] == "CANCELLED", stored_projection
    assert len(stored_projection["projection"]["canonical_hash"]) == 64, stored_projection
    assert stored_projection["projection"]["source_event_count"] == len(packet["events"]), stored_projection

    stale_packet = {"events": packet["events"][:-1]}
    stale_rebuild = httpx.post(
        f"{RECONCILER}/api/v1/projections/rebuild",
        json=stale_packet,
        timeout=20,
    )
    assert stale_rebuild.status_code == 409, stale_rebuild.text
    assert stale_rebuild.json()["detail"]["error"] == "stale_projection_evidence", stale_rebuild.text

    semantic_mutation = json.loads(json.dumps(packet))
    semantic_mutation["events"][4]["payload"]["amount"] = "11999"
    changed_rebuild = httpx.post(
        f"{RECONCILER}/api/v1/projections/rebuild",
        json=semantic_mutation,
        timeout=20,
    )
    assert changed_rebuild.status_code == 409, changed_rebuild.text
    assert changed_rebuild.json()["detail"]["error"] == "stale_projection_evidence", changed_rebuild.text

    superset_packet = {
        "events": [
            *packet["events"],
            {
                "event_id": f"{aggregate}-capacity-proof",
                "aggregate_id": aggregate,
                "event_type": "CapacityRevised",
                "occurred_at": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
                "received_at": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
                "payload": {"revision": 2, "available_units": 40},
            },
        ]
    }
    superset_rebuild = expect(
        httpx.post(
            f"{RECONCILER}/api/v1/projections/rebuild",
            json=superset_packet,
            timeout=20,
        ),
        200,
    )
    assert superset_rebuild["projection"]["source_event_count"] == len(packet["events"]) + 1


def ops_console_flow() -> None:
    health = expect(httpx.get(f"{OPS}/health", timeout=10), 200)
    assert health["status"] == "ok", health

    demo = expect(
        httpx.post(f"{OPS}/api/demo/run/late-cancel", timeout=20),
        200,
    )
    repairs = demo["result"]["reconciliation"]["repairs"]
    assert "REVERSE_SETTLEMENT" in repairs, demo
    assert "REVERSE_REWARD" in repairs, demo


def celery_flow(pass_no: int) -> None:
    # Exercise the containerized worker from the host without importing the
    # source package by filesystem name (services/reconciler contains a hyphen).
    # The task name and Redis broker/result backend are the public integration
    # contract between the smoke test and the worker container.
    from celery import Celery

    redis_url = "redis://127.0.0.1:6379/0"
    client = Celery("pickup-pact-integration-smoke", broker=redis_url, backend=redis_url)

    _, packet = late_cancel_packet(100 + pass_no)
    async_result = client.send_task("app.tasks.reconcile_packet", args=[packet])
    result = async_result.get(timeout=45)
    assert "REVERSE_SETTLEMENT" in result["repairs"], result
    assert "REVERSE_REWARD" in result["repairs"], result


def one_pass(pass_no: int) -> None:
    commitment_flow(pass_no)
    capacity_concurrency_flow(pass_no)
    idempotency_concurrency_flow(pass_no)
    pact_financial_flow(pass_no)
    merchant_fulfillment_flow(pass_no)
    ledger_flow(pass_no)
    kafka_ledger_flow(pass_no)
    reconciler_flow(pass_no)
    ops_console_flow()
    celery_flow(pass_no)
    print(f"full topology integration pass {pass_no} succeeded")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--passes", type=int, default=2)
    args = parser.parse_args()

    wait_http(f"{COMMITMENT}/actuator/health", timeout_s=240)
    wait_http(f"{LEDGER}/actuator/health", timeout_s=240)
    wait_http(f"{MERCHANT}/actuator/health", timeout_s=240)
    wait_http(f"{RECONCILER}/health", timeout_s=240)
    wait_http(f"{RECONCILER}/ready", timeout_s=240)
    wait_http(f"{OPS}/health", timeout_s=240)

    for pass_no in range(1, args.passes + 1):
        one_pass(pass_no)


if __name__ == "__main__":
    main()
