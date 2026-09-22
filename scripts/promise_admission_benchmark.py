#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import random
import sys
from math import ceil
from pathlib import Path
from statistics import quantiles

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from demo.promise_admission import PromiseAdmissionInput, quote_promise


def simulate(
    *,
    orders: int,
    seed: int,
    arrival_rate_per_minute: float,
    service_rate_units_per_minute: float,
    cap_minutes: int,
    historical_quote_minutes: int,
) -> dict:
    rng = random.Random(seed)
    current_time = 0.0
    arrivals: list[tuple[float, int, float]] = []
    for _ in range(orders):
        current_time += rng.expovariate(arrival_rate_per_minute)
        units = rng.choices([1, 2, 3, 4], weights=[0.55, 0.30, 0.12, 0.03])[0]
        travel_minutes = rng.uniform(4.0, 14.0)
        arrivals.append((current_time, units, travel_minutes))

    def run(admission: bool) -> dict:
        workload_units = 0.0
        previous_time = 0.0
        lateness: list[float] = []
        accepted = 0
        paused = 0
        offered_later = 0
        arrival_aligned = 0
        max_backlog_minutes = 0.0

        for arrival_time, units, travel_minutes in arrivals:
            elapsed = arrival_time - previous_time
            workload_units = max(
                0.0,
                workload_units - service_rate_units_per_minute * elapsed,
            )
            previous_time = arrival_time
            actual_ready_minutes = (
                workload_units + units
            ) / service_rate_units_per_minute

            if admission:
                quote = quote_promise(
                    PromiseAdmissionInput(
                        backlog_units=ceil(workload_units),
                        order_units=units,
                        service_rate_units_per_minute=service_rate_units_per_minute,
                        travel_minutes=ceil(travel_minutes),
                        max_promise_minutes=cap_minutes,
                        safety_minutes=2,
                    )
                )
                if quote.decision.value == "PAUSE":
                    paused += 1
                    continue
                if quote.decision.value == "OFFER_LATER":
                    offered_later += 1
                else:
                    arrival_aligned += 1
                promised_minutes = float(quote.quoted_minutes)
            else:
                promised_minutes = max(
                    travel_minutes,
                    float(historical_quote_minutes),
                )

            accepted += 1
            lateness.append(max(0.0, actual_ready_minutes - promised_minutes))
            workload_units += units
            max_backlog_minutes = max(
                max_backlog_minutes,
                workload_units / service_rate_units_per_minute,
            )

        overpromised = sum(value > 1e-9 for value in lateness)
        p95 = quantiles(lateness, n=20)[18] if len(lateness) >= 20 else 0.0
        return {
            "accepted_orders": accepted,
            "accepted_rate": accepted / orders,
            "paused_orders": paused,
            "offered_later_orders": offered_later,
            "arrival_aligned_orders": arrival_aligned,
            "avoidable_overpromise_rate": overpromised / accepted if accepted else 0.0,
            "p95_lateness_minutes": p95,
            "average_lateness_minutes": sum(lateness) / accepted if accepted else 0.0,
            "max_backlog_minutes": max_backlog_minutes,
        }

    naive = run(False)
    admission = run(True)
    assert admission["avoidable_overpromise_rate"] < naive["avoidable_overpromise_rate"]
    assert admission["accepted_rate"] > 0.95
    assert admission["max_backlog_minutes"] <= cap_minutes

    return {
        "scope": (
            "Synthetic deterministic stress model. This is policy evidence, "
            "not production throughput, latency, or customer-impact evidence."
        ),
        "config": {
            "orders": orders,
            "seed": seed,
            "arrival_rate_per_minute": arrival_rate_per_minute,
            "service_rate_units_per_minute": service_rate_units_per_minute,
            "cap_minutes": cap_minutes,
            "historical_quote_minutes": historical_quote_minutes,
        },
        "accept_all": naive,
        "promise_admission": admission,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--orders", type=int, default=20000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--arrival-rate", type=float, default=0.65)
    parser.add_argument("--service-rate", type=float, default=1.2)
    parser.add_argument("--cap-minutes", type=int, default=16)
    parser.add_argument("--historical-quote-minutes", type=int, default=8)
    parser.add_argument("--output", default="/tmp/promise-admission-benchmark.json")
    args = parser.parse_args()

    result = simulate(
        orders=args.orders,
        seed=args.seed,
        arrival_rate_per_minute=args.arrival_rate,
        service_rate_units_per_minute=args.service_rate,
        cap_minutes=args.cap_minutes,
        historical_quote_minutes=args.historical_quote_minutes,
    )
    Path(args.output).write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
