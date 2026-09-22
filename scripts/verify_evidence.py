#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from copy import deepcopy
from pathlib import Path


def load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def without_runtime_fields(payload: dict) -> dict:
    cleaned = deepcopy(payload)
    cleaned.get("metrics", {}).pop("elapsed_ms", None)
    return cleaned


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark-actual", required=True)
    parser.add_argument("--matrix-actual", required=True)
    parser.add_argument("--policy-actual", required=True)
    parser.add_argument("--benchmark-expected", default="artifacts/consistency-benchmark.json")
    parser.add_argument("--matrix-expected", default="artifacts/consistency-matrix.json")
    parser.add_argument("--policy-expected", default="artifacts/pickup-policy-lab.json")
    args = parser.parse_args()

    expected_benchmark = without_runtime_fields(load(args.benchmark_expected))
    actual_benchmark = without_runtime_fields(load(args.benchmark_actual))
    if actual_benchmark != expected_benchmark:
        raise SystemExit(
            "seed-42 benchmark no longer matches committed evidence\n"
            f"expected={json.dumps(expected_benchmark, sort_keys=True)}\n"
            f"actual={json.dumps(actual_benchmark, sort_keys=True)}"
        )

    expected_matrix = load(args.matrix_expected)
    actual_matrix = load(args.matrix_actual)
    if actual_matrix != expected_matrix:
        raise SystemExit(
            "multi-seed benchmark no longer matches committed evidence\n"
            f"expected={json.dumps(expected_matrix, sort_keys=True)}\n"
            f"actual={json.dumps(actual_matrix, sort_keys=True)}"
        )

    expected_policy = load(args.policy_expected)
    actual_policy = load(args.policy_actual)
    if actual_policy != expected_policy:
        raise SystemExit(
            "scheduled pickup policy replay no longer matches committed evidence\n"
            f"expected={json.dumps(expected_policy, sort_keys=True)}\n"
            f"actual={json.dumps(actual_policy, sort_keys=True)}"
        )

    print("committed consistency and pickup-policy evidence reproduced successfully")


if __name__ == "__main__":
    main()
