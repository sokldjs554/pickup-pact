#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path


def run_policy_replay(
    *,
    orders: int,
    seed: int,
    capacity_units: int,
    batch_size: int,
    max_deferral_slots: int,
) -> dict:
    rng = random.Random(seed)
    slot_count = max(50, orders // 20)
    requests: list[list[int]] = [[] for _ in range(slot_count)]

    for _ in range(orders):
        sample = rng.random()
        units = 1 if sample < 0.55 else 2 if sample < 0.87 else 3
        requests[rng.randrange(slot_count)].append(units)

    baseline_used = [0] * slot_count
    baseline_accepted_orders = 0
    baseline_rejected_orders = 0
    baseline_accepted_units = 0

    # Baseline: four concurrent requests read the same stale slot snapshot.
    for slot, slot_requests in enumerate(requests):
        used = 0
        for offset in range(0, len(slot_requests), batch_size):
            batch = slot_requests[offset : offset + batch_size]
            snapshot = used
            accepted = [units for units in batch if snapshot + units <= capacity_units]
            used += sum(accepted)
            baseline_accepted_orders += len(accepted)
            baseline_rejected_orders += len(batch) - len(accepted)
            baseline_accepted_units += sum(accepted)
        baseline_used[slot] = used

    # Pickup Pact: admission is serialized by the atomic slot ledger. If the
    # selected slot is full, find a small number of later customer-visible slots
    # that can be re-offered; the real customer flow still requires explicit choice.
    pact_used = [0] * (slot_count + max_deferral_slots + 1)
    pact_selected_slot_orders = 0
    pact_later_slot_offerable_orders = 0
    pact_unserviceable_orders = 0
    pact_accepted_units = 0
    deferral_distances: list[int] = []

    for slot, slot_requests in enumerate(requests):
        for units in slot_requests:
            placed = False
            for distance in range(max_deferral_slots + 1):
                candidate = slot + distance
                if pact_used[candidate] + units <= capacity_units:
                    pact_used[candidate] += units
                    pact_accepted_units += units
                    if distance == 0:
                        pact_selected_slot_orders += 1
                    else:
                        pact_later_slot_offerable_orders += 1
                        deferral_distances.append(distance)
                    placed = True
                    break
            if not placed:
                pact_unserviceable_orders += 1

    baseline_utilization = round(
        100
        * sum(min(used, capacity_units) for used in baseline_used)
        / (capacity_units * slot_count),
        2,
    )
    pact_utilization = round(
        100
        * sum(pact_used[:slot_count])
        / (capacity_units * slot_count),
        2,
    )

    return {
        "benchmark": "synthetic-scheduled-pickup-policy-v1",
        "scope": (
            "Deterministic synthetic policy replay; not production demand, SLA, "
            "revenue, or real merchant traffic."
        ),
        "config": {
            "orders": orders,
            "seed": seed,
            "capacity_units_per_slot": capacity_units,
            "slot_count": slot_count,
            "baseline_snapshot_batch_size": batch_size,
            "max_deferral_slots": max_deferral_slots,
        },
        "baseline": {
            "accepted_orders": baseline_accepted_orders,
            "rejected_orders": baseline_rejected_orders,
            "accepted_units": baseline_accepted_units,
            "oversubscribed_units": sum(
                max(0, used - capacity_units) for used in baseline_used
            ),
            "overbooked_slots": sum(
                1 for used in baseline_used if used > capacity_units
            ),
            "mean_slot_utilization_pct": baseline_utilization,
        },
        "pickup_pact": {
            "offerable_orders_within_window": pact_selected_slot_orders + pact_later_slot_offerable_orders,
            "selected_slot_feasible_orders": pact_selected_slot_orders,
            "later_slot_offerable_orders": pact_later_slot_offerable_orders,
            "unserviceable_orders_within_window": pact_unserviceable_orders,
            "accepted_units": pact_accepted_units,
            "oversubscribed_units": sum(
                max(0, used - capacity_units) for used in pact_used
            ),
            "overbooked_slots": sum(
                1 for used in pact_used if used > capacity_units
            ),
            "mean_slot_utilization_pct": pact_utilization,
            "mean_reoffer_distance_slots": (
                round(sum(deferral_distances) / len(deferral_distances), 3)
                if deferral_distances
                else 0.0
            ),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--orders", type=int, default=20_000)
    parser.add_argument("--seed", type=int, default=20_260_922)
    parser.add_argument("--capacity-units", type=int, default=40)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--max-deferral-slots", type=int, default=2)
    parser.add_argument("--output", default="artifacts/pickup-policy-lab.json")
    args = parser.parse_args()

    payload = run_policy_replay(
        orders=args.orders,
        seed=args.seed,
        capacity_units=args.capacity_units,
        batch_size=args.batch_size,
        max_deferral_slots=args.max_deferral_slots,
    )
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
