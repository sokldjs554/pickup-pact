# Target-role traceability

This document makes the job-description mapping auditable instead of listing technologies without evidence.

| Target expectation | Concrete implementation/evidence |
|---|---|
| AI-driven PRD → design → code → docs | `ai/prompts/`, `ai/evals/cases.json`, `docs/ai-first-workflow.md`, n8n/Make templates |
| Kotlin | `commitment-service` aggregate/application/API/adapters |
| Java | `ledger-service` balanced financial posting domain |
| Python | FastAPI reconciliation, Celery tasks, benchmark and verification scripts |
| Spring | both JVM services |
| WebFlux | reactive commitment command edge and R2DBC/Redis adapters |
| FastAPI | canonical replay/reconciliation API |
| Flask | operator replay console |
| PostgreSQL | aggregate, outbox, append-only ledger and reconciliation audit schema; idempotency enforced by unique event keys |
| MongoDB | immutable raw-envelope archive adapter |
| Redis | atomic capacity lease Lua path + Celery broker/cache topology |
| Elasticsearch | searchable incident/reconciliation index adapter |
| Kafka | transactional-outbox publish path + financial-event consumer contract |
| Celery | asynchronous replay worker and retry/backoff policy |
| DDD | Pickup Commitment + Financial Ledger bounded contexts and invariants |
| EDA | versioned domain envelopes and AsyncAPI contract |
| CQRS | command-owned invariants; disposable projections/rebuild repair |
| Distributed consistency | outbox, at-least-once delivery, event-level idempotency, compensation |
| REST/OpenAPI | explicit OpenAPI 3.1 contract, idempotency header on commitment writes |
| SQL tuning | replay/timeline indexes + `EXPLAIN (ANALYZE, BUFFERS)` artifact |
| Performance troubleshooting | deterministic race benchmark + HTTP load driver + runbook |
| Docker | four service images + local dependency topology |
| Kubernetes | deployments/services/probes/resources/HPA |
| AWS | Terraform blueprint for EKS/RDS/ElastiCache/MSK |
| Jenkins | verification/test/benchmark/container stages |
| Datadog | service/env tagging and anomaly monitor template |
| Elastic APM | trace propagation/instrumentation integration notes |
| Claude Code / Cursor / Claude / ChatGPT / Gemini | `CLAUDE.md`, `.cursor/rules/project.mdc`, `AGENTS.md`, provider-neutral review interface, prompts and eval contract; no fake external run claim |
| n8n / Make | regression/triage workflow artifacts |
| Slack / Jira / Notion | optional validated automation destinations; no live account connection claimed |
| Sprint/cross-functional workflow | `docs/sprint-brief.md` with PRD, handoff, QA, rollout and retrospective evidence plan |

## Interview story

The project is not presented as “I used many tools.” The primary story is one production-shaped failure mode: **a pickup promise crosses capacity, payment, settlement and rewards while events may be delayed, duplicated or reordered**. Every technology is assigned only where it supports that failure boundary.
