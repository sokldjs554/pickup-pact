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

- `outbox_events(aggregate_id, occurred_at, id)` for one commitment's event timeline;
- partial `outbox_events(occurred_at, id) where published_at is null` for relay polling;
- `ledger_batches(aggregate_id, created_at desc)` for financial history;
- `ledger_entries(event_id)` for batch entry lookup;
- `reconciliation_run(aggregate_id, created_at desc)` for incident history.

The release gate now starts the real PostgreSQL 16 Compose service and runs `scripts/capture_postgres_plan.py`. The script seeds a target aggregate plus noise rows, executes `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`, requires the planner to use `idx_outbox_aggregate_timeline`, and captures both the normal indexed plan and a forced-sequential baseline as `postgres-plan-evidence.json`.

Execution times in that artifact are **CI-container measurements only**. They are useful for plan analysis and regression evidence, not production latency claims.

## Load tooling

`scripts/load_http.py` and `scripts/load_matrix.py` can rerun the loopback test. Any new environment-specific numbers should be archived and described with their scope before being quoted in a portfolio.
