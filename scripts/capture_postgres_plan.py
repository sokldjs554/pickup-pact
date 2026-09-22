#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg


DSN = "postgresql://pickuppact:pickuppact@127.0.0.1:5432/pickuppact"
INDEX_NAME = "idx_outbox_aggregate_timeline"


def walk_plan(node: dict):
    yield node
    for child in node.get("Plans", []):
        yield from walk_plan(child)


def run_explain(cursor, aggregate_id: str, *, force_seq: bool) -> dict:
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="/tmp/postgres-plan-evidence.json")
    parser.add_argument("--noise-rows", type=int, default=5000)
    parser.add_argument("--target-rows", type=int, default=100)
    args = parser.parse_args()

    target = str(uuid.uuid4())
    base = datetime.now(UTC)
    rows = []
    for i in range(args.target_rows):
        rows.append((
            str(uuid.uuid4()),
            target,
            "CommitmentConfirmed",
            json.dumps({"n": i, "kind": "target"}),
            base + timedelta(milliseconds=i),
        ))
    for i in range(args.noise_rows):
        rows.append((
            str(uuid.uuid4()),
            str(uuid.uuid4()),
            "CommitmentConfirmed",
            json.dumps({"n": i, "kind": "noise"}),
            base + timedelta(milliseconds=args.target_rows + i),
        ))

    with psycopg.connect(DSN) as connection:
        with connection.cursor() as cursor:
            cursor.executemany(
                """
                insert into outbox_events(id, aggregate_id, event_type, payload, occurred_at)
                values (%s::uuid, %s::uuid, %s, %s::jsonb, %s)
                """,
                rows,
            )
            cursor.execute("analyze outbox_events")
            indexed = run_explain(cursor, target, force_seq=False)
            sequential = run_explain(cursor, target, force_seq=True)

            index_nodes = [
                node for node in walk_plan(indexed["Plan"])
                if node.get("Index Name") == INDEX_NAME
            ]
            if not index_nodes:
                raise AssertionError(
                    f"expected {INDEX_NAME} in plan, got "
                    f"{[(n.get('Node Type'), n.get('Index Name')) for n in walk_plan(indexed['Plan'])]}"
                )

            evidence = {
                "scope": "CI PostgreSQL 16 container; synthetic plan evidence, not production latency",
                "target_aggregate_id": target,
                "target_rows": args.target_rows,
                "noise_rows": args.noise_rows,
                "expected_index": INDEX_NAME,
                "index_used": True,
                "indexed": {
                    "planning_time_ms": indexed.get("Planning Time"),
                    "execution_time_ms": indexed.get("Execution Time"),
                    "plan": indexed["Plan"],
                },
                "forced_sequential_baseline": {
                    "planning_time_ms": sequential.get("Planning Time"),
                    "execution_time_ms": sequential.get("Execution Time"),
                    "plan": sequential["Plan"],
                },
            }
            Path(args.output).write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(json.dumps({
                "index_used": evidence["index_used"],
                "expected_index": INDEX_NAME,
                "indexed_execution_ms": evidence["indexed"]["execution_time_ms"],
                "forced_seq_execution_ms": evidence["forced_sequential_baseline"]["execution_time_ms"],
            }, ensure_ascii=False))


if __name__ == "__main__":
    main()
