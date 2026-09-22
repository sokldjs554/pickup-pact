# PaytaLab Backend Developer — JD gap audit

Checked against the current Wanted **Backend Developer** posting and PaytaLab engineering/career guidance in September 2026.

This audit separates **implemented evidence** from **blueprints or non-live external integrations**.

| JD item | Status | Repository evidence | Remaining limitation |
|---|---|---|---|
| AI used from design through development | Strong | `ai/prompts/`, `ai/evals/`, `docs/ai-first-workflow.md`, provider-neutral review boundary | Does not claim every provider ran on every commit |
| PM / designer / frontend collaboration and sprint cycle | Partial | `docs/sprint-brief.md`, OpenAPI/AsyncAPI handoff contracts, QA/release gates | Solo portfolio cannot replace real multi-person workplace collaboration |
| MSA service development | Strong | Kotlin commitment service, Java ledger service, Python reconciler, Compose/K8s topology | Public Render demo intentionally runs a reduced sandbox |
| Domain-event loose coupling | Strong | AsyncAPI, outbox, Kafka consumers, event envelopes, reconciliation tests | — |
| Kafka async processing | Strong | outbox relay, Kafka topology, integration smoke | — |
| CQRS | Strong | canonical projection rebuild/query path, PostgreSQL projection | — |
| Redis caching / coordination strategy | Strong | atomic capacity lease via Redis Lua + Celery broker topology | Public demo does not boot Redis |
| Distributed transaction / consistency problems | Strong | idempotency, conflict quarantine, compensation, late-event canonical replay | — |
| Datadog monitoring / proactive incident response | Partial | `infra/observability/datadog-monitor.json`, runbook/monitor contracts | No live Datadog tenant production claim |
| Kotlin / Python / Java | Strong | Kotlin commitment, Python reconciler/demo, Java ledger | — |
| Spring / WebFlux / FastAPI / Flask | Strong | executable service modules and tests | — |
| PostgreSQL / MongoDB / Redis / Elasticsearch | Strong for code/topology | persistence adapters + Docker runtime topology | Not all run in the public Render demo |
| Kafka / Celery | Strong | event topology + replay worker | — |
| AWS / Kubernetes / Docker / Jenkins | Strong as deployable blueprint | Terraform, K8s manifests, Docker images, Jenkinsfile | No live AWS/EKS production claim |
| Elastic APM | Partial | instrumentation/config integration evidence | No live external APM tenant claim |
| Slack / Jira / Notion | Partial | optional automation destinations documented | No live SaaS account integration claim |
| Data modeling for performance / scalability | Strong | schema indexes, projection/outbox/ledger models | — |
| Query execution-plan analysis | Strong | release gate runs real PostgreSQL 16 `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` and asserts `idx_outbox_aggregate_timeline` usage | Synthetic CI dataset, not production latency |
| RESTful API design | Strong | OpenAPI 3.1, command/query APIs, explicit errors and idempotency boundaries | — |
| Proactive problem discovery / solution proposal | Strong | pickup-promise protection, hidden cancellation recovery, Trust Receipt, one-time Pickup Code | Based on public product/JD context and synthetic scenarios, not private company data |
| Understand purpose/context and finish work | Strong portfolio evidence | repeated release gate, exact-SHA live smoke, docs/implementation contract checks | Workplace behavior is still a hiring judgement |
| Communication / grow together | Partial | design rationale, sprint handoff, readable contracts/docs | Solo repository cannot substitute for real team references |

## Current posting — item-by-item

### Main responsibilities

The current Backend Developer posting emphasizes:
- AI-first development from design through implementation;
- close work with PM, designers and frontend engineers;
- rapid sprint cycles from kickoff through QA, release and retrospective;
- MSA with domain events;
- Kafka, CQRS, Redis and distributed consistency;
- Datadog-based monitoring and proactive incident response.

### Development stack

The current posting lists:
- Kotlin, Python, Java;
- Spring, WebFlux, FastAPI, Flask;
- PostgreSQL, MongoDB, Redis, Elasticsearch;
- Kafka, Celery;
- AWS, Kubernetes, Docker, Jenkins;
- Datadog, Elastic APM;
- Slack, Jira, Notion;
- Claude Code, Claude, ChatGPT, Gemini, n8n and Make.

Each of those names has a concrete artifact or implementation path in this repository. External SaaS/cloud operation is only claimed where evidence exists.

### Qualifications

The current posting explicitly asks for:
- AI development tools / prompt engineering or strong intent to use them;
- problem-first use of Kotlin, Python or Java;
- scalable data modeling and execution-plan-based query optimization;
- RESTful API design and iterative improvement;
- ownership, communication, context awareness and proactive problem solving.

### Preferred qualifications

The current Wanted crawl used for this audit does **not expose a separate “우대사항” section** for the Backend Developer posting. This file therefore does not invent one.

## Product differentiation

Pickup Pact is intentionally not another order CRUD / Kafka demo.

The customer route demonstrates:
1. store / menu / cart smart ordering;
2. adaptive pickup-promise protection;
3. hidden compensation when cancellation messages arrive late;
4. **Trust Receipt** derived from payment, pickup-time changes, cancellation and reversal evidence;
5. **single-use Pickup Code** backed by terminal `PickupClaimed`;
6. order and cancellation history while ledger/audit detail stays in dev mode.

The interview story is: **customer trust first; distributed consistency is the backend mechanism, not the UI.**
