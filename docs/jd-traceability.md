# Target-role traceability

This document makes the job-description mapping auditable instead of listing technologies without evidence.

| Target expectation | Concrete implementation/evidence |
|---|---|
| AI-driven PRD → design → code → docs | `ai/prompts/`, `ai/evals/cases.json`, `docs/ai-first-workflow.md`, n8n/Make templates |
| Kotlin | `commitment-service` aggregate/application/API/adapters |
| Java | `merchant-fulfillment-service` durable intake/JIT workflow + `ledger-service` balanced financial posting and idempotency |
| Python | FastAPI reconciliation, Celery tasks, benchmark and verification scripts |
| Spring | commitment, merchant fulfillment and ledger JVM services |
| WebFlux | reactive commitment command edge and R2DBC/Redis adapters |
| FastAPI | canonical replay/reconciliation API plus the public synthetic customer/catalog/order demo API |
| Flask | operator replay console |
| PostgreSQL | commitment/outbox, merchant inbox/delivery/effects/JIT state, append-only ledger, reconciliation audit |
| MongoDB | distinct event-delivery evidence archive; conflicting copies of one event ID are preserved |
| Redis | customer-visible 5-minute slot availability, Lua-protected atomic reservation, per-lease idempotent release + Celery broker topology |
| Elasticsearch | searchable incident/reconciliation index adapter |
| Kafka | commitment outbox → merchant intake, fulfillment feedback → Pickup Pact, financial event consumers |
| Celery | asynchronous replay worker and retry/backoff policy |
| DDD | Pickup Commitment + Merchant Fulfillment + Financial Ledger bounded contexts and invariants |
| EDA | versioned domain envelopes and AsyncAPI contract |
| CQRS | command-owned invariants + explicit PostgreSQL projection rebuild/query API from canonical event-time replay |
| Distributed consistency | outbox, merchant inbox dedupe, durable ACK delivery, deduplicated intent records, event idempotency, conflict quarantine, compensation |
| Customer requirements → product | customer flow is store/menu/cart → server-authoritative workload quote → signed feasible slots → customer time selection → idempotent hold → payment/confirmation → pickup/cancellation/receipt; Chromium E2E verifies the journey |
| Scheduled pickup + promise protection | customer chooses a feasible capacity-backed slot before payment; later capacity revision → `RESLOT_REVIEW` → customer-friendly new-time proposal → `PickupRescheduled` |
| Merchant order intake | `CommitmentConfirmed` → DB inbox dedupe → durable `ORDER_AVAILABLE` delivery; ACK-before/after reconnect behavior is exercised in full-topology integration |
| JIT preparation | pickup time + workload → earliest start / target ready / latest ready; too-early start is rejected; EARLY/LATE READY and post-preparation cancellation/reschedule races are explicit evidence |
| Deduplicated intent records | duplicate Kafka event ID does not create a second POS-print or notification intent row; actual device/provider execution is not claimed |
| Fulfillment → promise feedback | `READY_LATE → FulfillmentAnomalyDetected → PickupPactBreached → REWARD 500 PTS`, duplicate late signals do not double-compensate |
| Pickup Pact Guarantee | versioned `PickupPactIssued → PickupPactRenegotiated → PickupPactBreached` lifecycle is persisted in the Kotlin aggregate/PostgreSQL/outbox; breach derives a deterministic 500P Kafka ledger posting; browser/API evidence covers the customer view |
| Customer trust layer | customer adapter validates a one-time pickup code; core Kotlin commitment enforces `CONFIRMED → PICKED_UP`, emits `PickupClaimed`, releases capacity exactly once; mobile Trust Receipt and order history keep backend terminology hidden |
| Order/payment/settlement/reward domains | core order/pickup + payment authorization + settlement/reward ledger are implemented; Pact breach traverses outbox/Kafka into the reward ledger. A separate promotion/coupon campaign domain is not claimed. |
| REST/OpenAPI | OpenAPI 3.1 contract for signed quote → Idempotency-Key hold → payment → confirm/reschedule/breach/cancel/claim; runtime topology tests assert 400 vs 409 state semantics |
| SQL tuning | CI/release-gate capture PostgreSQL 16 `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` for both event history and the merchant active-pickup schedule, requiring the outbox timeline index and a partial `(store_id, pickup_at, id)` schedule index; forced-sequential baselines are kept as evidence |
| Performance troubleshooting | deterministic race benchmark + loopback HTTP baseline + runbook |
| Docker | commitment, ledger, reconciler, ops-console, and interviewer-demo images |
| Kubernetes | checked-in deployments/services/probes/resource limits for the service topology |
| AWS | Terraform blueprint for RDS PostgreSQL, ElastiCache Redis, and MSK Serverless; Kubernetes manifests are separate and no live EKS deployment is claimed |
| Jenkins | verification/test/benchmark/container stages |
| Datadog | service/env tagging and anomaly monitor template |
| Elastic APM | trace propagation/instrumentation integration notes |
| Claude Code / Cursor / Claude / ChatGPT / Gemini | `CLAUDE.md`, `.cursor/rules/project.mdc`, `AGENTS.md`, provider-neutral review interface, prompts and eval contract; no fake provider-run claim |
| n8n / Make | regression/triage workflow artifacts |
| Slack / Jira / Notion | optional automation destinations; no live account connection claimed |
| Sprint/cross-functional workflow | `docs/sprint-brief.md` with PRD, handoff, QA, rollout and retrospective evidence plan |

## Current repair-review evidence

| Requirement | Bounded implementation |
|---|---|
| Domain rules | Full-cancellation/single-posting scope; partial or unattributed amounts are blocked, not guessed |
| API correctness | Strict request and response schemas, 403/404/409/415/422 tests, mounted-router checks |
| Retry and recovery | Evidence-version/hash-bound approval, 24 concurrent requests, forced process termination before/after commit |
| Honest comparison | Original 25/65; independent conservative reference and candidate 65/65 on the documented 13-case corpus; no superiority claim |

## Interview story

The current primary deliverable is the evidence-bound repair workbench: **identify when cancellation/settlement evidence is sufficient, wait or block when it is not, and approve only the saved target, amount, unit and evidence version**. The prior merchant-fulfillment implementation remains a supporting scenario, not a claim of market novelty or production-grade cancellation authority.

The existing root URL keeps the customer/merchant demo. `/repair-lab` exposes fixed synthetic evidence and an approval journal; `/?dev=1` keeps the older console. Both are simulations. Technical evidence is traceable to code, contracts, repeated tests and clearly labeled infrastructure blueprints.


Full current-posting audit: [jd-audit-2026-09-22.md](jd-audit-2026-09-22.md)
Customer feedback mapping: [customer-feedback-to-product.md](customer-feedback-to-product.md)
AI iteration evidence: [ai-iteration-log.md](ai-iteration-log.md)


## Verification boundary correction — 2026-09-23

Merchant `POS_PRINT` and notification effects in this repository are durable **intent records**, not a physical printer driver or an external notification provider. The tested unique constraint prevents duplicate intent rows; exactly-once physical printing/delivery is not claimed. The repair workbench is a separate, bounded synthetic approval journal. It does not resolve the authority race between cancellation approval and physical preparation, execute real refunds, or implement partial-refund allocation. The current README and repair-workbench document are the scope reference; earlier implementation-history descriptions are not a broader completion claim.
