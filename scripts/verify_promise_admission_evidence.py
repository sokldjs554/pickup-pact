#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--actual", required=True)
    parser.add_argument(
        "--expected",
        default="artifacts/promise-admission-benchmark.json",
    )
    args = parser.parse_args()

    expected = json.loads(Path(args.expected).read_text())
    actual = json.loads(Path(args.actual).read_text())
    if actual != expected:
        raise SystemExit(
            "promise admission evidence drifted\n"
            f"expected={json.dumps(expected, sort_keys=True)}\n"
            f"actual={json.dumps(actual, sort_keys=True)}"
        )
    print("promise admission evidence reproduced exactly")


if __name__ == "__main__":
    main()
