# Public demo

The public demo is a **session-isolated smart-order recovery sandbox** with two UX layers.

## Product recovery experience

The default public route is rendered as a product-style **order protection and recovery center**, not an operator console. The dark backend sidebar is not present in the default experience. A compact product header, a protected-order preview, and recovery-focused cards explain the system through business state rather than infrastructure terminology.

A first-time visitor presses **복구 시나리오 체험하기** once. The browser then drives the real demo API through the representative incident:

1. create, pay, and confirm a normal 9,000 KRW pickup order;
2. record a cancellation that reaches the server 52 seconds late;
3. post the incorrect 9,000 KRW settlement and 90P reward while that cancellation is delayed;
4. run the real reconciliation engine and surface a product-level warning.

The recovery center shows the order summary, a five-step status strip, the incorrect financial state, and a plain-language explanation. The visitor then chooses **잘못된 처리 되돌리기**. The result changes from `settlement 9,000 KRW / reward 90P` to `settlement 0 KRW / reward 0P`, while the UI states that the original history remains preserved.

Backend navigation becomes visible only after the reviewer explicitly selects **개발자 구현 보기** or **개발자 화면 열기**.

## Technical detail layer

Reviewers who want implementation detail can use the same session to inspect:

- manual order lifecycle operations;
- pickup-capacity revision;
- late cancellation, exact Kafka redelivery, conflicting payload, and capacity-drop labs;
- the real `services/reconciler/app/engine.py` output;
- receive-time ordering versus business-time ordering;
- deterministic repair proposals and evidence IDs;
- reversal ledger batches and the operator audit trail.

Each browser stores its own demo session ID. The Render instance keeps only in-memory synthetic demo state; it is not connected to real merchants, customers, or payment providers.

## Representative late-cancellation flow

1. pickup is held, paid, and confirmed;
2. cancellation occurs first but is recorded with a later `received_at`;
3. settlement and reward are posted before that cancellation delivery arrives;
4. reconciliation detects that settlement/reward happened after an earlier business-time cancellation;
5. it proposes `REVERSE_SETTLEMENT` and `REVERSE_REWARD`;
6. applying the plan in the sandbox appends reversal batches rather than deleting the original ledger evidence;
7. net settlement and reward balance return to zero.

A projection rebuild is proposed only when receive-order folding actually differs from canonical business-time folding. It is not hard-coded into every late-cancellation case.

## Public-demo boundary

The public demo packages the real reconciliation engine but intentionally does not boot Kafka, PostgreSQL, Redis, MongoDB, Elasticsearch, both Spring services, and Celery on the free Render process. Those integrations are represented by the service code, Compose topology, contracts, infrastructure manifests, tests, and evidence artifacts in the repository.

All data is synthetic.
