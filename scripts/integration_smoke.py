#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx


COMMITMENT = "http://127.0.0.1:8080"
LEDGER = "http://127.0.0.1:8081"
RECONCILER = "http://127.0.0.1:8000"
OPS = "http://127.0.0.1:5000"


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

    cancelled = expect(
        httpx.post(
            f"{COMMITMENT}/api/v1/commitments/{commitment_id}/cancel",
            timeout=15,
        ),
        200,
    )
    assert cancelled["state"] == "CANCELLED", cancelled


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
