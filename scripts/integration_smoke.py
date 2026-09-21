#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx


COMMITMENT = "http://127.0.0.1:8080"
LEDGER = "http://127.0.0.1:8081"
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
            if response.status_code < 500:
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
    pickup_at = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    held = expect(
        httpx.post(
            f"{COMMITMENT}/api/v1/commitments/hold",
            json={
                "storeId": f"integration-store-{pass_no}",
                "pickupAt": pickup_at,
                "units": 2,
            },
            timeout=15,
        ),
        200,
    )
    assert held["state"] == "HELD", held
    assert held["paymentAuthorized"] is False, held
    commitment_id = held["id"]
    lease_key = held["leaseToken"].split("|", 1)[0]

    import redis
    redis_client = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
    assert redis_client.exists(lease_key) == 1, held

    paid = expect(
        httpx.post(
            f"{COMMITMENT}/api/v1/commitments/{commitment_id}/authorize-payment",
            json={"authorizationId": f"auth-integration-{pass_no}"},
            timeout=15,
        ),
        200,
    )
    assert paid["paymentAuthorized"] is True, paid

    confirmed = expect(
        httpx.post(
            f"{COMMITMENT}/api/v1/commitments/{commitment_id}/confirm",
            timeout=15,
        ),
        200,
    )
    assert confirmed["state"] == "CONFIRMED", confirmed

    tracked = expect(
        httpx.get(
            f"{COMMITMENT}/api/v1/commitments/{commitment_id}",
            timeout=15,
        ),
        200,
    )
    assert tracked["id"] == commitment_id, tracked
    assert tracked["state"] == "CONFIRMED", tracked

    cancelled = expect(
        httpx.post(
            f"{COMMITMENT}/api/v1/commitments/{commitment_id}/cancel",
            timeout=15,
        ),
        200,
    )
    assert cancelled["state"] == "CANCELLED", cancelled
    assert redis_client.exists(lease_key) == 0, {
        "lease_key": lease_key,
        "cancelled": cancelled,
    }

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
                return total >= 4 and published == total

    wait_until(
        outbox_published,
        timeout_s=30,
        label="commitment outbox delivery to Kafka",
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

    conflicts = expect(
        httpx.get(f"{LEDGER}/api/v1/ledger/conflicts?limit=20", timeout=15),
        200,
    )
    assert any(
        item["eventId"] == event_id
        and item["incomingFingerprint"] != item["existingFingerprint"]
        for item in conflicts
    ), conflicts


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
    # Import after setting localhost broker/backend for the host-side client.
    os.environ["REDIS_URL"] = "redis://127.0.0.1:6379/0"
    from services.reconciler.app.tasks import celery_app

    _, packet = late_cancel_packet(100 + pass_no)
    async_result = celery_app.send_task("app.tasks.reconcile_packet", args=[packet])
    result = async_result.get(timeout=45)
    assert "REVERSE_SETTLEMENT" in result["repairs"], result
    assert "REVERSE_REWARD" in result["repairs"], result


def one_pass(pass_no: int) -> None:
    commitment_flow(pass_no)
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
    wait_http(f"{RECONCILER}/health", timeout_s=240)
    wait_http(f"{OPS}/health", timeout_s=240)

    for pass_no in range(1, args.passes + 1):
        one_pass(pass_no)


if __name__ == "__main__":
    main()
