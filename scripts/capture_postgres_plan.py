#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg


DSN = "postgresql://pickuppact:pickuppact@127.0.0.1:5432/pickuppact"
OUTBOX_INDEX = "idx_outbox_aggregate_timeline"
STORE_SCHEDULE_INDEX = "idx_pickup_commitments_store_schedule"
MERCHANT_DELIVERY_INDEX = "idx_merchant_deliveries_pending"


def connect_after_schema(timeout_s: int = 45):
    """Survive the postgres image's temporary init server and final restart."""
    deadline = time.time() + timeout_s
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            connection = psycopg.connect(DSN, connect_timeout=3)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    select
                      to_regclass('public.pickup_commitments') is not null,
                      to_regclass('public.outbox_events') is not null,
                      to_regclass('public.merchant_orders') is not null
                    """
                )
                if all(cursor.fetchone()):
                    return connection
            connection.close()
        except psycopg.OperationalError as exc:
            last_error = exc
        time.sleep(0.5)
    raise RuntimeError(f"postgres schema did not become stable: {last_error!r}")


def walk_plan(node: dict):
    yield node
    for child in node.get("Plans", []):
        yield from walk_plan(child)


def require_index(plan: dict, expected_index: str) -> None:
    nodes = list(walk_plan(plan["Plan"]))
    if not any(node.get("Index Name") == expected_index for node in nodes):
        raise AssertionError(
            f"expected {expected_index} in plan, got "
            f"{[(node.get('Node Type'), node.get('Index Name')) for node in nodes]}"
        )


def explain_outbox(cursor, aggregate_id: str, *, force_seq: bool) -> dict:
    mode = "off" if force_seq else "on"
    cursor.execute(f"set enable_indexscan = {mode}")
    cursor.execute(f"set enable_bitmapscan = {mode}")
    cursor.execute("set enable_seqscan = on")
    cursor.execute(
        """
        explain (analyze, buffers, format json)
        select id as event_id, event_type, occurred_at, payload
        from outbox_events
        where aggregate_id = %s::uuid
        order by occurred_at, id
        """,
        (aggregate_id,),
    )
    return cursor.fetchone()[0][0]


def explain_store_schedule(cursor, store_id: str, start: datetime, end: datetime, *, force_seq: bool) -> dict:
    mode = "off" if force_seq else "on"
    cursor.execute(f"set enable_indexscan = {mode}")
    cursor.execute(f"set enable_bitmapscan = {mode}")
    cursor.execute("set enable_seqscan = on")
    cursor.execute(
        """
        explain (analyze, buffers, format json)
        select id, pickup_at, units, state
        from pickup_commitments
        where store_id = %s
          and state in ('HELD', 'CONFIRMED', 'AT_RISK')
          and pickup_at >= %s
          and pickup_at < %s
        order by pickup_at, id
        limit 100
        """,
        (store_id, start, end),
    )
    return cursor.fetchone()[0][0]


def explain_merchant_reconnect(cursor, store_id: str, *, force_seq: bool) -> dict:
    mode = "off" if force_seq else "on"
    cursor.execute(f"set enable_indexscan = {mode}")
    cursor.execute(f"set enable_bitmapscan = {mode}")
    cursor.execute("set enable_seqscan = on")
    cursor.execute(
        """
        explain (analyze, buffers, format json)
        select delivery_sequence, order_id, delivery_type, payload
        from merchant_deliveries
        where store_id = %s
          and delivery_sequence > 0
          and acknowledged_at is null
        order by delivery_sequence
        limit 50
        """,
        (store_id,),
    )
    return cursor.fetchone()[0][0]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="/tmp/postgres-plan-evidence.json")
    parser.add_argument("--noise-rows", type=int, default=5000)
    parser.add_argument("--target-rows", type=int, default=100)
    args = parser.parse_args()

    target_aggregate = str(uuid.uuid4())
    target_store = "store-plan-target"
    base = datetime.now(UTC)

    outbox_rows = []
    for i in range(args.target_rows):
        outbox_rows.append((
            str(uuid.uuid4()),
            target_aggregate,
            "CommitmentConfirmed",
            json.dumps({"n": i, "kind": "target"}),
            base + timedelta(milliseconds=i),
        ))
    for i in range(args.noise_rows):
        outbox_rows.append((
            str(uuid.uuid4()),
            str(uuid.uuid4()),
            "CommitmentConfirmed",
            json.dumps({"n": i, "kind": "noise"}),
            base + timedelta(milliseconds=args.target_rows + i),
        ))

    commitment_rows = []
    for i in range(args.target_rows):
        commitment_rows.append((
            str(uuid.uuid4()),
            target_store,
            base + timedelta(minutes=10, seconds=i),
            1 + (i % 3),
            f"plan-target-{i}",
            True,
            "CONFIRMED",
            f"plan-idem-target-{i}",
            f"plan-fingerprint-target-{i}",
        ))
    for i in range(args.noise_rows):
        commitment_rows.append((
            str(uuid.uuid4()),
            f"noise-store-{i % 200}",
            base + timedelta(minutes=i % 240),
            1 + (i % 3),
            f"plan-noise-{i}",
            bool(i % 2),
            "CANCELLED" if i % 5 == 0 else "CONFIRMED",
            f"plan-idem-noise-{i}",
            f"plan-fingerprint-noise-{i}",
        ))

    merchant_order_rows = []
    merchant_delivery_rows = []
    for i in range(args.target_rows):
        order_id = str(uuid.uuid4())
        pickup_at = base + timedelta(minutes=30, seconds=i)
        merchant_order_rows.append((
            order_id,
            target_store,
            pickup_at,
            1 + (i % 3),
            "RECEIVED",
            90,
            pickup_at - timedelta(minutes=4),
            pickup_at - timedelta(minutes=1),
            pickup_at + timedelta(minutes=3),
        ))
        merchant_delivery_rows.append((
            order_id,
            target_store,
            "ORDER_AVAILABLE",
            json.dumps({"n": i, "kind": "target"}),
            None,
        ))
    for i in range(args.noise_rows):
        order_id = str(uuid.uuid4())
        pickup_at = base + timedelta(minutes=i % 240)
        store_id = f"noise-store-{i % 200}"
        merchant_order_rows.append((
            order_id,
            store_id,
            pickup_at,
            1 + (i % 3),
            "READY" if i % 7 == 0 else "RECEIVED",
            90,
            pickup_at - timedelta(minutes=4),
            pickup_at - timedelta(minutes=1),
            pickup_at + timedelta(minutes=3),
        ))
        merchant_delivery_rows.append((
            order_id,
            store_id,
            "ORDER_AVAILABLE",
            json.dumps({"n": i, "kind": "noise"}),
            base if i % 3 == 0 else None,
        ))

    with connect_after_schema() as connection:
        with connection.cursor() as cursor:
            cursor.executemany(
                """
                insert into outbox_events(id, aggregate_id, event_type, payload, occurred_at)
                values (%s::uuid, %s::uuid, %s, %s::jsonb, %s)
                """,
                outbox_rows,
            )
            cursor.executemany(
                """
                insert into pickup_commitments(
                    id, store_id, pickup_at, units, lease_token,
                    payment_authorized, state, idempotency_key, request_fingerprint,
                    version, updated_at
                )
                values (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, 0, now())
                """,
                commitment_rows,
            )
            cursor.executemany(
                """
                insert into merchant_orders(
                    order_id, store_id, pickup_at, capacity_units, state,
                    preparation_seconds, earliest_start_at, target_ready_at, latest_ready_at,
                    version, updated_at
                )
                values (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, 0, now())
                """,
                merchant_order_rows,
            )
            cursor.executemany(
                """
                insert into merchant_deliveries(
                    order_id, store_id, delivery_type, payload, acknowledged_at
                )
                values (%s::uuid, %s, %s, %s::jsonb, %s)
                """,
                merchant_delivery_rows,
            )
            cursor.execute("analyze outbox_events")
            cursor.execute("analyze pickup_commitments")
            cursor.execute("analyze merchant_orders")
            cursor.execute("analyze merchant_deliveries")

            outbox_indexed = explain_outbox(cursor, target_aggregate, force_seq=False)
            outbox_sequential = explain_outbox(cursor, target_aggregate, force_seq=True)
            require_index(outbox_indexed, OUTBOX_INDEX)

            schedule_start = base
            schedule_end = base + timedelta(hours=2)
            schedule_indexed = explain_store_schedule(
                cursor,
                target_store,
                schedule_start,
                schedule_end,
                force_seq=False,
            )
            schedule_sequential = explain_store_schedule(
                cursor,
                target_store,
                schedule_start,
                schedule_end,
                force_seq=True,
            )
            require_index(schedule_indexed, STORE_SCHEDULE_INDEX)

            merchant_indexed = explain_merchant_reconnect(
                cursor,
                target_store,
                force_seq=False,
            )
            merchant_sequential = explain_merchant_reconnect(
                cursor,
                target_store,
                force_seq=True,
            )
            require_index(merchant_indexed, MERCHANT_DELIVERY_INDEX)

            evidence = {
                "scope": "CI PostgreSQL 16 container; synthetic plan evidence, not production latency",
                "target_rows": args.target_rows,
                "noise_rows": args.noise_rows,
                "outbox_timeline": {
                    "target_aggregate_id": target_aggregate,
                    "expected_index": OUTBOX_INDEX,
                    "index_used": True,
                    "indexed": {
                        "planning_time_ms": outbox_indexed.get("Planning Time"),
                        "execution_time_ms": outbox_indexed.get("Execution Time"),
                        "plan": outbox_indexed["Plan"],
                    },
                    "forced_sequential_baseline": {
                        "planning_time_ms": outbox_sequential.get("Planning Time"),
                        "execution_time_ms": outbox_sequential.get("Execution Time"),
                        "plan": outbox_sequential["Plan"],
                    },
                },
                "store_pickup_schedule": {
                    "target_store_id": target_store,
                    "expected_index": STORE_SCHEDULE_INDEX,
                    "index_used": True,
                    "window_start": schedule_start.isoformat(),
                    "window_end": schedule_end.isoformat(),
                    "indexed": {
                        "planning_time_ms": schedule_indexed.get("Planning Time"),
                        "execution_time_ms": schedule_indexed.get("Execution Time"),
                        "plan": schedule_indexed["Plan"],
                    },
                    "forced_sequential_baseline": {
                        "planning_time_ms": schedule_sequential.get("Planning Time"),
                        "execution_time_ms": schedule_sequential.get("Execution Time"),
                        "plan": schedule_sequential["Plan"],
                    },
                },
                "merchant_reconnect_delivery": {
                    "target_store_id": target_store,
                    "expected_index": MERCHANT_DELIVERY_INDEX,
                    "index_used": True,
                    "indexed": {
                        "planning_time_ms": merchant_indexed.get("Planning Time"),
                        "execution_time_ms": merchant_indexed.get("Execution Time"),
                        "plan": merchant_indexed["Plan"],
                    },
                    "forced_sequential_baseline": {
                        "planning_time_ms": merchant_sequential.get("Planning Time"),
                        "execution_time_ms": merchant_sequential.get("Execution Time"),
                        "plan": merchant_sequential["Plan"],
                    },
                },
            }
            Path(args.output).write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(json.dumps({
                "outbox_index": OUTBOX_INDEX,
                "outbox_indexed_execution_ms": evidence["outbox_timeline"]["indexed"]["execution_time_ms"],
                "outbox_forced_seq_execution_ms": evidence["outbox_timeline"]["forced_sequential_baseline"]["execution_time_ms"],
                "store_schedule_index": STORE_SCHEDULE_INDEX,
                "store_schedule_indexed_execution_ms": evidence["store_pickup_schedule"]["indexed"]["execution_time_ms"],
                "store_schedule_forced_seq_execution_ms": evidence["store_pickup_schedule"]["forced_sequential_baseline"]["execution_time_ms"],
                "merchant_delivery_index": MERCHANT_DELIVERY_INDEX,
                "merchant_delivery_indexed_execution_ms": evidence["merchant_reconnect_delivery"]["indexed"]["execution_time_ms"],
                "merchant_delivery_forced_seq_execution_ms": evidence["merchant_reconnect_delivery"]["forced_sequential_baseline"]["execution_time_ms"],
            }, ensure_ascii=False))


if __name__ == "__main__":
    main()
