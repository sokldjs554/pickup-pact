# Performance and consistency evidence

This repository separates **measured evidence** from deployment claims.

## Executed locally

`python3 scripts/consistency_benchmark.py --orders 20000 --seed 42`

The benchmark is synthetic. It measures correctness under:

- concurrent capacity snapshot races;
- duplicate deliveries;
- cancellations whose business time predates settlement/reward but whose receive time is later.

It does **not** represent production TPS, network latency, JVM performance, or real merchant traffic.

### Recorded seed-42 result

| Synthetic fault | Naive baseline | Pickup Pact path |
|---|---:|---:|
| Capacity oversubscribed units | 194 | 0 |
| Duplicate financial posts | 1,207 | 0 |
| Stale financial orders after late cancellation | 381 | 0 |
| Deterministic repair commands emitted | — | 762 |

The run simulated **20,000 orders / 101,588 events**. The zero values are properties of the modeled atomic/idempotent repair path in this deterministic benchmark; they are **not** production error-rate claims. The raw machine-readable artifact is `artifacts/consistency-benchmark.json`.

### Multi-seed robustness check

A second matrix runs seeds `11,22,33,44,55` at 20,000 orders each. Across **100,000 synthetic orders / 507,602 events**, the naive model accumulated **1,036 oversubscribed capacity units, 5,896 duplicate financial posts, and 1,706 stale financial orders after late cancellation**. The modeled atomic/idempotent path remained at `0` for those three modeled fault classes and emitted `3,412` deterministic repair commands. Raw per-seed data is in `artifacts/consistency-matrix.json`.

Again, this demonstrates algorithmic behavior under the simulator's assumptions; it is not evidence of production incident rates or end-to-end throughput.

## Executed loopback FastAPI baseline

A repeated local HTTP baseline was also executed against the real FastAPI `POST /api/v1/reconcile` route after a warm-up. Each concurrency level ran **2,000 requests × 3 repetitions**; the table reports the median value across the three runs.

Environment recorded by the benchmark: Python `3.13.5`, Linux `6.18.44`, 5 logical CPUs. Persistence was disabled, so this test excludes PostgreSQL, Redis, Kafka, MongoDB and Elasticsearch.

| Concurrency | Failures | Median run throughput | Median run p95 | Median run p99 |
|---:|---:|---:|---:|---:|
| 10 | 0 | 666.01 req/s | 36.80 ms | 135.17 ms |
| 50 | 0 | 753.71 req/s | 157.57 ms | 282.90 ms |
| 100 | 0 | 833.32 req/s | 148.04 ms | 203.92 ms |

Raw repetitions are stored in `artifacts/reconciler-http-matrix.json`. The run-to-run spread is visible in that artifact (especially at concurrency 50), so these numbers are a **loopback service baseline, not a production capacity claim**. A deployment-facing performance claim would require the same matrix with real PostgreSQL/Redis/Kafka persistence, container/network overhead, sustained-duration load, warm/cold separation, and query/consumer-lag evidence.

## Database tuning plan

`sql/explain/commitment_timeline.sql` uses PostgreSQL `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` for the operator timeline query. Candidate indexes are defined in `sql/schema.sql`:

- `(aggregate_id, occurred_at, event_id)` for canonical replay;
- partial outbox index on unpublished rows;
- `(store_id, slot_start)` for lease/commitment lookup;
- `financial_event_receipt(source_event_id)` for semantic idempotency/conflict detection, plus a ledger uniqueness backstop on `source_event_id + account_code + direction`.

A real PostgreSQL plan should be checked before claiming any query latency improvement.

## Load plan

`scripts/load_http.py` can drive the FastAPI reconciliation endpoint with configurable concurrency. Results should be archived under `artifacts/runtime/` and only then quoted in a portfolio.
