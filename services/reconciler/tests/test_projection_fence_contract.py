"""SQL boundary unit tests. Real PostgreSQL coverage lives in verify_upgrade_boundaries.py."""
from contextlib import nullcontext
import sys
from types import SimpleNamespace

import pytest

from app import persistence
from app.engine import reconcile
from app.models import ReconcileRequest
from test_repair_safety import prefix


def database_transport(monkeypatch, rowcount=1):
    calls = []
    cursor = SimpleNamespace(rowcount=rowcount, execute=lambda sql, params: calls.append((sql, params)))
    connection = SimpleNamespace(cursor=lambda: nullcontext(cursor))
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(
        connect=lambda *args, **kwargs: nullcontext(connection)
    ))
    monkeypatch.setenv("POSTGRES_DSN", "test-only-projection-transport")
    return calls


@pytest.mark.parametrize("predicate", [
    "commitment_projection.source_event_count > 0",
    "commitment_projection.source_event_count = cardinality(commitment_projection.source_event_ids)",
    "commitment_projection.source_event_fingerprints ?& commitment_projection.source_event_ids",
])
def test_existing_projection_requires_complete_nonempty_provenance(monkeypatch, predicate):
    calls = database_transport(monkeypatch)
    persistence.rebuild_projection(reconcile(ReconcileRequest(events=prefix())))
    sql = " ".join(calls[0][0].split())
    assert predicate in sql


def test_fence_rejection_does_not_return_successful_snapshot(monkeypatch):
    database_transport(monkeypatch, rowcount=0)
    with pytest.raises(persistence.StaleProjectionEvidence):
        persistence.rebuild_projection(reconcile(ReconcileRequest(events=prefix())))


def test_new_projection_still_carries_all_ids_fingerprints_and_count(monkeypatch):
    database_transport(monkeypatch)
    result = reconcile(ReconcileRequest(events=prefix()))
    snapshot = persistence.rebuild_projection(result)
    assert snapshot["source_event_ids"] == result.source_event_ids
    assert snapshot["source_event_fingerprints"] == result.source_event_fingerprints
    assert snapshot["source_event_count"] == len(result.source_event_ids) == 3
