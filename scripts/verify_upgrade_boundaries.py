#!/usr/bin/env python3
"""Exercise retained pre-allocation/fence records against the real Docker APIs.

Seeds only uniquely named synthetic audit rows in the local integration database.
No schema changes, existing-row cleanup, external service or payment execution.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import uuid

import httpx
import psycopg

DSN = "postgresql://pickuppact:pickuppact@127.0.0.1:5432/pickuppact"
LEDGER = "http://127.0.0.1:8081/api/v1/ledger/postings"
PROJECTION = "http://127.0.0.1:8000/api/v1/projections/rebuild"


def seed_legacy_posting(order: str, event: str, reason: str, amount: int, unit: str,
                        source: str | None = None) -> None:
    fingerprint = hashlib.sha256(f"{order}|{reason}|{amount}|{unit}".encode()).hexdigest()
    with psycopg.connect(DSN) as connection:
        connection.execute(
            "insert into ledger_batches(event_id,semantic_fingerprint,aggregate_id,reason,source_event_id) "
            "values(%s,%s,%s,%s,%s)",
            (event, fingerprint, order, reason, source),
        )
        for direction in ("DEBIT", "CREDIT"):
            connection.execute(
                "insert into ledger_entries(event_id,account,direction,amount,currency,occurred_at) "
                "values(%s,%s,%s,%s,%s,now())",
                (event, "legacy-audit-" + direction.lower(), direction, amount, unit),
            )


def legacy_posting_retries() -> None:
    for reason, amount, unit in (("SETTLEMENT", 9000, "KRW"), ("REWARD", 90, "PTS")):
        order = "upgrade-retry-" + uuid.uuid4().hex
        event = order + "-posting"
        seed_legacy_posting(order, event, reason, amount, unit)
        payload = dict(eventId=event, aggregateId=order, type=reason, amount=amount)
        for _ in range(2):
            response = httpx.post(LEDGER, json=payload, timeout=15)
            assert response.status_code == 200, (response.status_code, response.text)
            assert response.json()["result"] == "DUPLICATE_NOOP", response.text
        conflicting = httpx.post(LEDGER, json={**payload, "amount": amount + 1}, timeout=15)
        assert conflicting.status_code == 409, conflicting.text
        with psycopg.connect(DSN) as connection:
            assert connection.execute(
                "select count(*) from ledger_entries where event_id=%s", (event,)
            ).fetchone()[0] == 2


def unattributed_legacy_reversal_blocks_new_reversal() -> None:
    for reason, reverse, amount, unit in (
        ("SETTLEMENT", "REVERSE_SETTLEMENT", 9000, "KRW"),
        ("REWARD", "REVERSE_REWARD", 90, "PTS"),
    ):
        for missing_source in (None, "", "   "):
            order = "upgrade-unattributed-" + uuid.uuid4().hex
            source = order + "-source"
            seed_legacy_posting(order, source, reason, amount, unit)
            seed_legacy_posting(order, order + "-legacy-reversal", reverse, amount, unit, missing_source)
            new_event = order + "-new-reversal"
            response = httpx.post(LEDGER, json=dict(
                eventId=new_event, aggregateId=order, type=reverse, amount=1, sourceEventId=source
            ), timeout=15)
            assert response.status_code == 409, (response.status_code, response.text)
            assert response.json()["result"] == "SOURCE_POSTING_CONFLICT", response.text
            with psycopg.connect(DSN) as connection:
                assert connection.execute(
                    "select count(*) from ledger_batches where event_id=%s", (new_event,)
                ).fetchone()[0] == 0


def attributed_reversals_remain_scoped_and_idempotent() -> None:
    # An ambiguous legacy reversal cannot freeze a different order or unit.
    for reason, reverse, unit, other_reverse, other_unit in (
        ("SETTLEMENT", "REVERSE_SETTLEMENT", "KRW", "REVERSE_REWARD", "PTS"),
        ("REWARD", "REVERSE_REWARD", "PTS", "REVERSE_SETTLEMENT", "KRW"),
    ):
        order = "upgrade-scoped-" + uuid.uuid4().hex
        source = order + "-source"
        seed_legacy_posting(order, source, reason, 10, unit)
        seed_legacy_posting(order + "-other", order + "-outside", reverse, 10, unit)
        seed_legacy_posting(order, order + "-other-unit", other_reverse, 10, other_unit)
        payload = dict(eventId=order + "-repair", aggregateId=order, type=reverse,
                       amount=10, sourceEventId=source)
        first = httpx.post(LEDGER, json=payload, timeout=15)
        assert first.status_code == 200 and first.json()["result"] == "POSTED", first.text
        retry = httpx.post(LEDGER, json=payload, timeout=15)
        assert retry.status_code == 200 and retry.json()["result"] == "DUPLICATE_NOOP", retry.text
        excess = httpx.post(LEDGER, json={**payload, "eventId": order + "-excess", "amount": 1}, timeout=15)
        assert excess.status_code == 409 and excess.json()["result"] == "SOURCE_POSTING_CONFLICT", excess.text
        with psycopg.connect(DSN) as connection:
            assert connection.execute(
                "select count(*) from ledger_entries where event_id=%s", (payload["eventId"],)
            ).fetchone()[0] == 2


def legacy_projection_without_provenance_is_not_overwritten() -> None:
    from app.engine import reconcile
    from app.models import ReconcileRequest

    for layout in ("no_provenance", "missing_fingerprints", "wrong_count"):
        order = "upgrade-projection-" + uuid.uuid4().hex
        events = [dict(
            event_id=f"{order}-{index}", aggregate_id=order, event_type=kind,
            occurred_at=f"2026-09-23T00:00:0{index}Z", received_at=f"2026-09-23T00:00:0{index}Z",
            payload={},
        ) for index, kind in enumerate(("PickupSlotHeld", "PaymentAuthorized", "CommitmentConfirmed"))]
        result = reconcile(ReconcileRequest.model_validate({"events": events}))
        ids = [] if layout == "no_provenance" else result.source_event_ids
        fingerprints = result.source_event_fingerprints if layout == "wrong_count" else {}
        count = 0 if layout == "no_provenance" else 2 if layout == "wrong_count" else 3
        with psycopg.connect(DSN) as connection:
            connection.execute(
                "insert into commitment_projection(aggregate_id,status,payment_authorized,settled,rewarded,"
                "capacity_revision,settlement_post_count,reward_post_count,canonical_hash,"
                "source_event_ids,source_event_fingerprints,source_event_count) "
                "values(%s,'CANCELLED',true,false,false,0,0,0,%s,%s,%s::jsonb,%s)",
                (order, "1" * 64, ids, json.dumps(fingerprints), count),
            )
        response = httpx.post(PROJECTION, json={"events": events}, timeout=15)
        assert response.status_code == 409, (layout, response.status_code, response.text)
        assert response.json()["detail"]["error"] == "stale_projection_evidence", response.text
        with psycopg.connect(DSN) as connection:
            row = connection.execute(
                "select status,canonical_hash,source_event_count,source_event_ids,source_event_fingerprints "
                "from commitment_projection where aggregate_id=%s", (order,),
            ).fetchone()
            assert row == ("CANCELLED", "1" * 64, count, ids, fingerprints), row


def different_financial_plans_have_distinct_persisted_audit_ids() -> None:
    from app.engine import reconcile
    from app.models import ReconcileRequest
    from app.persistence import record_reconciliation

    os.environ["POSTGRES_DSN"] = DSN
    order = "upgrade-audit-" + uuid.uuid4().hex
    ids = []
    for amount in (3000, 6000):
        events = [dict(
            event_id=f"{order}-{index}", aggregate_id=order, event_type=kind,
            occurred_at=f"2026-09-23T00:00:0{index}Z", received_at=f"2026-09-23T00:00:0{index}Z",
            payload={"amount": str(amount)} if kind == "SettlementPosted" else {},
        ) for index, kind in enumerate(("PickupSlotHeld", "PaymentAuthorized", "CommitmentConfirmed",
                                        "CommitmentCancelled", "SettlementPosted"))]
        result = reconcile(ReconcileRequest.model_validate({"events": events}))
        assert result.financial_actions[0].amount == amount
        ids.append(record_reconciliation(result))
    assert ids[0] != ids[1], ids
    with psycopg.connect(DSN) as connection:
        assert connection.execute(
            "select count(*) from reconciliation_run where aggregate_id=%s", (order,)
        ).fetchone()[0] == 2


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--passes", type=int, default=2)
    parser.add_argument("--output", type=Path, default=Path("/tmp/upgrade-boundary-evidence.json"))
    args = parser.parse_args()
    assert args.passes > 0
    results = []
    checks = (legacy_posting_retries, unattributed_legacy_reversal_blocks_new_reversal,
              attributed_reversals_remain_scoped_and_idempotent,
              legacy_projection_without_provenance_is_not_overwritten,
              different_financial_plans_have_distinct_persisted_audit_ids)
    for pass_no in range(1, args.passes + 1):
        for check in checks:
            try:
                check()
                row = dict(pass_no=pass_no, check=check.__name__, status="passed")
            except Exception as exc:
                row = dict(pass_no=pass_no, check=check.__name__, status="failed", error=repr(exc))
            results.append(row)
            print(json.dumps(row), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2) + "\n")
    if any(row["status"] == "failed" for row in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
