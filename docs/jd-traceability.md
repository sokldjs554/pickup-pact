# Target-role traceability

This document makes the job-description mapping auditable instead of listing technologies without evidence.

| Target expectation | Concrete implementation/evidence |
|---|---|
| AI-driven PRD → design → code → docs | `ai/prompts/`, `ai/evals/cases.json`, `docs/ai-first-workflow.md`, n8n/Make templates |
| Kotlin | `commitment-service` aggregate/application/API/adapters |
| Java | `ledger-service` balanced financial posting, idempotency, conflict quarantine and history query domain |
| Python | FastAPI reconciliation, Celery tasks, benchmark and verification scripts |
| Spring | both JVM services |
| WebFlux | reactive commitment command edge and R2DBC/Redis adapters |
| FastAPI | canonical replay/reconciliation API plus the public synthetic customer/catalog/order demo API |
| Flask | operator replay console |
| PostgreSQL | commitment state, outbox, append-only ledger, reconciliation-run audit |
| MongoDB | distinct event-delivery evidence archive; conflicting copies of one event ID are preserved |
| Redis | customer-visible 5-minute slot availability, Lua-protected atomic reservation, per-lease idempotent release + Celery broker topology |
| Elasticsearch | searchable incident/reconciliation index adapter |
| Kafka | transactional-outbox relay + financial-event consumer contract |
| Celery | asynchronous replay worker and retry/backoff policy |
| DDD | Pickup Commitment + Financial Ledger bounded contexts and invariants |
| EDA | versioned domain envelopes and AsyncAPI contract |
| CQRS | command-owned invariants + explicit PostgreSQL projection rebuild/query API from canonical event-time replay |
| Distributed consistency | outbox, at-least-once delivery, event-level idempotency, conflict quarantine, compensation |
| Customer requirements → product | customer flow is store/menu/cart → server-authoritative workload quote → signed feasible slots → customer time selection → idempotent hold → payment/confirmation → pickup/cancellation/receipt; Chromium E2E verifies the journey |
| Scheduled pickup + promise protection | customer chooses a feasible capacity-backed slot before payment; later capacity revision → `RESLOT_REVIEW` → customer-friendly new-time proposal → `PickupRescheduled` |
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

## Interview story

The project is not presented as “I used many tools.” The primary story is one production-shaped failure mode: **a pickup promise crosses capacity, payment, settlement and rewards while events may be delayed, duplicated or reordered**.

The public URL behaves as a customer smart-order product first. Backend recovery and operator controls are not exposed to the customer route; reviewers enter them separately through `/?dev=1`. Technical evidence remains traceable to code, contracts, tests, measured artifacts, or clearly labeled blueprints.


Full current-posting audit: [jd-audit-2026-09-22.md](jd-audit-2026-09-22.md)
Customer feedback mapping: [customer-feedback-to-product.md](customer-feedback-to-product.md)
AI iteration evidence: [ai-iteration-log.md](ai-iteration-log.md)
