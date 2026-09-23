"""The persisted audit key must distinguish evidence and exact financial plans."""
from contextlib import nullcontext
import sys
from types import SimpleNamespace

import pytest

from app import persistence
from app.engine import reconcile
from app.models import ReconcileRequest
from test_repair_safety import cancelled, event


@pytest.fixture
def audit_database(monkeypatch):
    # Replace only database transport; exercise the real digest and INSERT path.
    writes = []
    cursor = SimpleNamespace(execute=lambda sql, params: writes.append(params))
    connection = SimpleNamespace(cursor=lambda: nullcontext(cursor))
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(
        connect=lambda *args, **kwargs: nullcontext(connection)
    ))
    monkeypatch.setenv("POSTGRES_DSN", "test-only-audit-transport")
    return writes


def planned(amount):
    return reconcile(ReconcileRequest(events=cancelled() + [
        event("settle-1", "SettlementPosted", 4, payload={"amount": str(amount)})
    ]))


def test_audit_identity_distinguishes_exact_reversal_amounts(audit_database):
    first, second = planned(3000), planned(6000)
    assert first.canonical_state == second.canonical_state
    assert first.evidence_event_ids == second.evidence_event_ids
    assert first.financial_actions[0].amount == 3000
    assert second.financial_actions[0].amount == 6000

    first_id = persistence.record_reconciliation(first)
    second_id = persistence.record_reconciliation(second)

    assert first_id != second_id
    assert audit_database[0][0] != audit_database[1][0]


def test_audit_identity_preserves_changed_source_evidence(audit_database):
    original = cancelled()
    changed = [e.model_copy(update={"payload": {"reason": "different-reason"}})
               if e.event_id == "cancel" else e for e in original]
    first = reconcile(ReconcileRequest(events=original))
    second = reconcile(ReconcileRequest(events=changed))
    assert first.canonical_state == second.canonical_state
    assert first.repairs == second.repairs
    assert first.source_event_fingerprints != second.source_event_fingerprints

    assert persistence.record_reconciliation(first) != persistence.record_reconciliation(second)


def test_audit_identity_is_stable_for_reordered_identical_evidence(audit_database):
    events = cancelled() + [event("settle-1", "SettlementPosted", 4, payload={"amount": "3000"})]
    first = reconcile(ReconcileRequest(events=events))
    second = reconcile(ReconcileRequest(events=list(reversed(events))))
    assert persistence.record_reconciliation(first) == persistence.record_reconciliation(second)
