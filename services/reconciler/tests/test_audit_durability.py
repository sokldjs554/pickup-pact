"""Disk-backed subprocess termination probes, not mocked database outcomes."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

# Existing CI may set only PYTHONPATH=services/reconciler.
ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.repair_audit.cases import suite
from scripts.repair_audit.reference import DurableExecutor, candidate_plan, reference_plan

PLANNERS = {"reference": reference_plan, "candidate": candidate_plan}


@pytest.mark.parametrize("planner", PLANNERS)
@pytest.mark.parametrize("point", ["after_inbox", "before_commit", "after_commit"])
def test_abrupt_process_exit_then_retry_produces_one_intent_per_kind(tmp_path, planner, point):
    case = next(c for c in suite() if c.name == "delayed_cancel")
    db = tmp_path / "evidence.sqlite"
    packet = tmp_path / "request.json"
    packet.write_text(case.request.model_dump_json())
    # Initialize before subprocess; avoid conflating schema setup with crash probe.
    DurableExecutor(db)
    code = '''
import sys
from pathlib import Path
from app.models import ReconcileRequest
from scripts.repair_audit.reference import DurableExecutor,candidate_plan,reference_plan
request=ReconcileRequest.model_validate_json(Path(sys.argv[2]).read_text())
planner=reference_plan if sys.argv[3]=='reference' else candidate_plan
DurableExecutor(sys.argv[1]).apply(request,planner,crash=sys.argv[4])
'''
    outcome = subprocess.run([sys.executable, "-c", code, str(db), str(packet), planner, point],
                             env=os.environ.copy(), capture_output=True, text=True, timeout=15)
    assert outcome.returncode == 77, outcome.stderr
    recovered = DurableExecutor(db)
    assert len(recovered.effects()) == (2 if point == "after_commit" else 0)
    for _ in range(3):
        recovered.apply(case.request, PLANNERS[planner])
    assert recovered.effects() == [("audit-order", "REVERSE_REWARD"),
                                   ("audit-order", "REVERSE_SETTLEMENT")]


@pytest.mark.parametrize("planner", PLANNERS)
def test_concurrent_exact_replays_share_one_durable_business_effect(tmp_path, planner):
    case = next(c for c in suite() if c.name == "delayed_cancel")
    executor = DurableExecutor(tmp_path / "race.sqlite")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: executor.apply(case.request, PLANNERS[planner]), range(24)))
    assert all(result.decision == "AUTO" for result in results)
    assert len(executor.effects()) == 2


@pytest.mark.parametrize("planner", PLANNERS)
def test_missing_evidence_survives_restart_then_replay_resolves(tmp_path, planner):
    missing = next(c for c in suite() if c.name == "missing_parent")
    restored = next(c for c in suite() if c.name == "parent_recovered")
    # Match posting occurrence between the two delivery packets; only the cause
    # is late, not a rewritten event. Keep the input immutable across retries.
    missing_request = restored.request.model_copy(update={
        "events": [e for e in restored.request.events if e.event_id != "late"]})
    db = tmp_path / "recovery.sqlite"
    first = DurableExecutor(db)
    assert first.apply(missing_request, PLANNERS[planner]).decision == "WAIT_FOR_EVIDENCE"
    assert first.effects() == []
    restarted = DurableExecutor(db)
    result = restarted.apply(restored.request, PLANNERS[planner])
    assert result.decision == "AUTO"
    assert restarted.effects() == [("audit-order", "REVERSE_SETTLEMENT")]


@pytest.mark.parametrize("planner", PLANNERS)
def test_conflicting_terminal_evidence_never_creates_durable_financial_intent(tmp_path, planner):
    case = next(c for c in suite() if c.name == "terminal_conflict")
    executor = DurableExecutor(tmp_path / "conflict.sqlite")
    result = executor.apply(case.request, PLANNERS[planner])
    assert result.decision == "MANUAL_REVIEW"
    assert executor.effects() == []
