# Public demo

The public demo is a **session-isolated smart-order recovery sandbox** with two UX layers.

## First-time recruiter story

The default landing page explains the business problem before exposing infrastructure terms. The expert navigation is hidden until a reviewer explicitly chooses **백엔드 구현 보기**.

A first-time visitor presses **직접 확인해보기** once. The browser then drives the real demo API through the representative story automatically:

1. create, pay, and confirm a normal 9,000 KRW pickup order;
2. record a cancellation that reaches the server 52 seconds late;
3. post the incorrect 9,000 KRW settlement and 90P reward while that cancellation is delayed;
4. run the real reconciliation engine and show the detected inconsistency.

At that point the visitor has one meaningful action: **문제 복구하기**. Applying it appends the deterministic reversal records and changes the visible result from `settlement 9,000 KRW / reward 90P` to `settlement 0 KRW / reward 0P`.

The general-user layer therefore presents **problem → detection → before/after recovery** first. It does not require the visitor to understand Kafka, event IDs, ledgers, or reconciliation terminology before seeing the value of the system.

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
