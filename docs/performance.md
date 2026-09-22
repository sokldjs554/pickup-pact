# Performance and consistency evidence

This repository separates **measured evidence** from deployment claims.

## Synthetic consistency benchmark

Command:

`python3 scripts/consistency_benchmark.py --orders 20000 --seed 42`

It measures modeled correctness under concurrent capacity snapshot races, duplicate deliveries, and late cancellations. It does **not** represent production TPS, network latency, JVM performance, or real merchant traffic.

| Synthetic fault | Naive baseline | Pickup Pact path |
|---|---:|---:|
| Capacity oversubscribed units | 194 | 0 |
| Duplicate financial posts | 1,207 | 0 |
| Stale financial orders after late cancellation | 381 | 0 |
| Deterministic repair commands emitted | — | 762 |

The run simulated **20,000 orders / 101,588 events**. Machine-readable evidence is committed at `artifacts/consistency-benchmark.json`.

A second matrix runs seeds `11,22,33,44,55` at 20,000 orders each. Across **100,000 synthetic orders / 507,602 events**, the naive model accumulated **1,036 oversubscribed capacity units, 5,896 duplicate financial posts, and 1,706 stale financial orders after late cancellation**. The modeled Pickup Pact path remained at `0` for those three fault classes and emitted `3,412` deterministic repair commands. Per-seed evidence is committed at `artifacts/consistency-matrix.json`.

These zero values are properties of the simulator and implemented atomic/idempotent model, not production incident-rate claims.

## Scheduled pickup policy lab

Command:

`python3 scripts/pickup_policy_lab.py --orders 20000 --seed 20260922`

This is a deterministic synthetic replay of the **same 20,000 requested orders** through two admission policies. It is not real PassOrder demand, production revenue, or an SLA claim.

The baseline models four concurrent requests reading the same stale slot snapshot before writes become visible. Pickup Pact uses an atomic capacity ledger and, when the selected slot is full, searches the next two customer-visible five-minute slots that could be offered back to the customer. The real product flow still requires the customer to choose that later time.

| Policy metric | Stale-snapshot baseline | Pickup Pact |
|---|---:|---:|
| Baseline admitted / Pickup Pact offerable within window | 19,751 | 20,000 |
| Baseline rejected / no feasible slot in window | 249 | 0 |
| Orders feasible in originally selected slot | — | 19,468 |
| Orders requiring a later-slot re-offer | — | 532 |
| Oversubscribed capacity units | 213 | 0 |
| Overbooked slots | 96 | 0 |
| Mean slot utilization | 77.03% | 78.59% |

For this synthetic workload, Pickup Pact avoided overbooking while **532 / 20,000** requests required a later feasible slot to be offered; the mean re-offer distance was one five-minute slot. The important point is the trade-off: correctness is not presented as free. Customers may need to choose a later slot when the original one no longer fits.

Machine-readable evidence is committed at `artifacts/pickup-policy-lab.json` and is regenerated in CI and the repeated release gate.

## Loopback FastAPI baseline

A prior local loopback run exercised the real `POST /api/v1/reconcile` route after warm-up. Each concurrency level ran **2,000 requests × 3 repetitions**. Persistence was disabled, so PostgreSQL, Redis, Kafka, MongoDB, and Elasticsearch were excluded.

Environment recorded for that run: Python `3.13.5`, Linux `6.18.44`, 5 logical CPUs.

| Concurrency | Failures | Median run throughput | Median run p95 | Median run p99 |
|---:|---:|---:|---:|---:|
| 10 | 0 | 666.01 req/s | 36.80 ms | 135.17 ms |
| 50 | 0 | 753.71 req/s | 157.57 ms | 282.90 ms |
| 100 | 0 | 833.32 req/s | 148.04 ms | 203.92 ms |

Only the recorded summary is committed at `artifacts/reconciler-http-summary.json`; the raw per-request samples are not claimed to be preserved. Treat this as a local service baseline, not production capacity evidence.

## Database tuning path

`sql/explain/commitment_timeline.sql` now targets the actual `outbox_events` schema and can be run with PostgreSQL `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`.

Relevant committed indexes are:

- `pickup_commitments(store_id, pickup_at, id) where state in (HELD, CONFIRMED, AT_RISK)` for a merchant-facing upcoming pickup schedule;
- `outbox_events(aggregate_id, occurred_at, id)` for one commitment's event timeline;
- partial `outbox_events(occurred_at, id) where published_at is null` for relay polling;
- `ledger_batches(aggregate_id, created_at desc)` for financial history;
- `ledger_entries(event_id)` for batch entry lookup;
- `reconciliation_run(aggregate_id, created_at desc)` for incident history.

The release gate now starts the real PostgreSQL 16 Compose service and runs `scripts/capture_postgres_plan.py`. The script seeds synthetic target/noise data and executes `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` for **two paths**: an aggregate event timeline and a store's active pickup schedule. It requires `idx_outbox_aggregate_timeline` and `idx_pickup_commitments_store_schedule` respectively, then captures both indexed plans and forced-sequential baselines as `postgres-plan-evidence.json`.

Execution times in that artifact are **CI-container measurements only**. They are useful for plan analysis and regression evidence, not production latency claims.

## Load tooling

`scripts/load_http.py` and `scripts/load_matrix.py` can rerun the loopback test. Any new environment-specific numbers should be archived and described with their scope before being quoted in a portfolio.
