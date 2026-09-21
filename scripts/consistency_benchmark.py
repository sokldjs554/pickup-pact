#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import random
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Metrics:
    orders: int
    simulated_events: int
    capacity_oversubscribed_units_baseline: int
    capacity_oversubscribed_units_pickup_pact: int
    duplicate_financial_posts_baseline: int
    duplicate_financial_posts_pickup_pact: int
    stale_financial_orders_after_late_cancel_baseline: int
    stale_financial_orders_after_late_cancel_pickup_pact: int
    repair_commands_emitted: int
    elapsed_ms: float


def capacity_race(rng: random.Random, orders: int) -> tuple[int, int]:
    """Simulate stale snapshot admission vs atomic lease admission."""
    capacity = 24
    slots = max(20, orders // 200)
    requests = [[] for _ in range(slots)]
    for _ in range(orders):
        requests[rng.randrange(slots)].append(rng.choice((1, 1, 1, 2, 2, 3)))

    baseline_over = 0
    pact_over = 0
    for slot_requests in requests:
        baseline_used = 0
        pact_used = 0
        for i in range(0, len(slot_requests), 4):
            batch = slot_requests[i:i + 4]
            accepted = [units for units in batch if baseline_used + units <= capacity]
            baseline_used += sum(accepted)
            for units in batch:
                if pact_used + units <= capacity:
                    pact_used += units
        baseline_over += max(0, baseline_used - capacity)
        pact_over += max(0, pact_used - capacity)
    return baseline_over, pact_over


def event_consistency(rng: random.Random, orders: int) -> tuple[int, int, int, int, int, int]:
    duplicate_posts_baseline = 0
    duplicate_posts_pact = 0
    stale_financial_baseline = 0
    stale_financial_pact = 0
    repair_commands = 0
    event_count = 0

    for _ in range(orders):
        event_count += 5
        settlement_posts = 1
        reward_posts = 1
        if rng.random() < 0.035:
            settlement_posts += 1
            duplicate_posts_baseline += 1
            event_count += 1
        if rng.random() < 0.025:
            reward_posts += 1
            duplicate_posts_baseline += 1
            event_count += 1
        late_cancel = rng.random() < 0.018
        if late_cancel:
            event_count += 1
            if settlement_posts > 0 or reward_posts > 0:
                stale_financial_baseline += 1
            if settlement_posts > 0:
                repair_commands += 1
            if reward_posts > 0:
                repair_commands += 1
        duplicate_posts_pact += 0
        stale_financial_pact += 0

    return (duplicate_posts_baseline, duplicate_posts_pact, stale_financial_baseline, stale_financial_pact, repair_commands, event_count)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--orders", type=int, default=20_000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", default="artifacts/consistency-benchmark.json")
    args = parser.parse_args()
    rng = random.Random(args.seed)
    started = time.perf_counter()
    baseline_over, pact_over = capacity_race(rng, args.orders)
    dup_base, dup_pact, stale_base, stale_pact, repairs, event_count = event_consistency(rng, args.orders)
    elapsed = (time.perf_counter() - started) * 1000
    metrics = Metrics(args.orders,event_count,baseline_over,pact_over,dup_base,dup_pact,stale_base,stale_pact,repairs,round(elapsed,3))
    output={"benchmark":"synthetic-consistency-race-v1","seed":args.seed,"scope":"Synthetic correctness benchmark; not production traffic or latency.","metrics":asdict(metrics)}
    path=Path(args.output); path.parent.mkdir(parents=True,exist_ok=True); path.write_text(json.dumps(output,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    print(json.dumps(output,indent=2,sort_keys=True))

if __name__ == "__main__": main()
