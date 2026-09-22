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
| Redis | Lua-protected atomic pickup-slot capacity counter + Celery broker topology |
| Elasticsearch | searchable incident/reconciliation index adapter |
| Kafka | transactional-outbox relay + financial-event consumer contract |
| Celery | asynchronous replay worker and retry/backoff policy |
| DDD | Pickup Commitment + Financial Ledger bounded contexts and invariants |
| EDA | versioned domain envelopes and AsyncAPI contract |
| CQRS | command-owned invariants + explicit PostgreSQL projection rebuild/query API from canonical event-time replay |
| Distributed consistency | outbox, at-least-once delivery, event-level idempotency, conflict quarantine, compensation |
| Customer requirements → product | public virtual-customer flow: store selection, menu/cart quantity changes, order/payment/confirmation, pickup status and cancellation; Chromium E2E verifies the journey |
| Promise admission before order creation | Kotlin + Python deterministic policy evaluates backlog, order size, service rate and customer travel time; returns `ACCEPT / OFFER_LATER / PAUSE`; shared golden cases prevent semantic drift |
| Pickup promise protection | capacity revision → `RESLOT_REVIEW` → customer-friendly new-time proposal → `PickupRescheduled`; preserves backend evidence while minimizing customer friction |
| Customer trust layer | customer adapter validates a one-time pickup code; core Kotlin commitment enforces `CONFIRMED → PICKED_UP`, emits `PickupClaimed`, releases capacity exactly once; mobile Trust Receipt and order history keep backend terminology hidden |
| Order/payment/settlement/reward domains | customer checkout exercises order/payment/confirmation; hidden cancellation race exercises settlement/reward reconciliation and compensation |
| REST/OpenAPI | explicit OpenAPI 3.1 contract for hold → payment authorization → confirm/cancel + tracking, ledger posting/history/conflicts, reconciliation |
| SQL tuning | PR CI and release-gate both capture PostgreSQL 16 `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` and require `idx_outbox_aggregate_timeline`; indexed and forced-sequential plans are uploaded as evidence |
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
