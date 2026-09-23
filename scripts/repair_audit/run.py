"""Run identical labelled traces through old core, reference, and patched core.

Run: PYTHONPATH=services/reconciler python -m scripts.repair_audit.run
"""
from __future__ import annotations
import argparse
from dataclasses import asdict
import hashlib
import importlib.util
import json
from pathlib import Path
import random
from time import perf_counter_ns

from app.models import ReconcileRequest
from .cases import suite
from .reference import Outcome, candidate_plan, reference_plan, FINANCIAL

ROOT = Path(__file__).resolve().parents[2]


def old_plan(request):
    module = old_plan.module
    result = module.reconcile(request)
    # Old results express manual review through repairs, not a decision field.
    decision = "MANUAL_REVIEW" if "MANUAL_REVIEW" in result.repairs else "AUTO"
    return Outcome(decision, frozenset(FINANCIAL & set(result.repairs)), result.canonical_state.status)


def initialize_old():
    path = ROOT / "artifacts/repair-audit/original_engine.py"
    actual = hashlib.sha1(b"blob " + str(path.stat().st_size).encode() + b"\0" + path.read_bytes()).hexdigest()
    if actual != "9279781d92a8f2db270f2cce734e2df7a90d5b41":
        raise RuntimeError("Original engine blob hash does not match pinned GitHub source")
    spec = importlib.util.spec_from_file_location("app._old_evidence_audit", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    old_plan.module = module


def benchmark(seeds: list[int]) -> dict:
    initialize_old()
    planners = {"original_801755bb": old_plan,
                "conservative_reference": reference_plan,
                "patched_core": candidate_plan}
    records = []
    for case in suite():
        for seed in seeds:
            events = list(case.request.events)
            random.Random(seed).shuffle(events)
            request = ReconcileRequest(events=events)
            for name, planner in planners.items():
                start = perf_counter_ns()
                result = planner(request)
                elapsed = perf_counter_ns() - start
                actual = set(result.repairs)
                extra = actual - case.repairs
                missed = case.repairs - actual
                records.append({
                    "case": case.name, "seed": seed, "implementation": name,
                    "expected_decision": case.decision, "decision": result.decision,
                    "expected_repairs": sorted(case.repairs), "repairs": sorted(actual),
                    "unexpected_financial_proposals": sorted(extra),
                    "missing_required_repairs": sorted(missed),
                    "correct": result.decision == case.decision and not extra and not missed,
                    "planner_time_us": round(elapsed / 1000, 3),
                    "status": result.status,
                })
    summary = {}
    for name in planners:
        rows = [r for r in records if r["implementation"] == name]
        summary[name] = {
            "traces": len(rows),
            "correct_decisions": sum(r["correct"] for r in rows),
            "unsafe_proposal_traces": sum(bool(r["unexpected_financial_proposals"]) for r in rows),
            "missed_repair_traces": sum(bool(r["missing_required_repairs"]) for r in rows),
            "wait_traces": sum(r["decision"] == "WAIT_FOR_EVIDENCE" for r in rows),
            "manual_review_traces": sum(r["decision"] == "MANUAL_REVIEW" for r in rows),
        }
    canonical = [{k:v for k,v in r.items() if k != "planner_time_us"} for r in records]
    return {
        "scope": "13 synthetic decision scenarios; same complete trace per planner; NOT production workload, Kafka/PSP testing or competitor performance",
        "source_commit": "801755bb422cd50ecbbf2fa6c0e275ff97fa9e75",
        "seeds": seeds, "scenarios": len(suite()), "summary": summary,
        "semantic_sha256": hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest(),
        "records": records,
        "cases": [{"name":c.name,"description":c.description,
                   "input":c.request.model_dump(mode="json"),
                   "expected_decision":c.decision,"expected_repairs":sorted(c.repairs)} for c in suite()],
    }


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--output", default="artifacts/repair-audit/comparison.json")
    args=parser.parse_args()
    result=benchmark([11,22,33,44,55])
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n")
    print(json.dumps({"scenarios":result["scenarios"],"summary":result["summary"],
                      "semantic_sha256":result["semantic_sha256"]}, indent=2))
    for name in ("conservative_reference", "patched_core"):
        if result["summary"][name]["correct_decisions"] != result["summary"][name]["traces"]:
            raise SystemExit(f"{name} violated a declared oracle case; inspect comparison.json")


if __name__ == "__main__":
    main()
