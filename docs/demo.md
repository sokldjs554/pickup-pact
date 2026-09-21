# Public demo

The public demo is a **session-isolated smart-order recovery sandbox** with two UX layers.

## First-time guided flow

The default landing page starts from a blank session and explains the business problem before exposing infrastructure terms. A visitor can finish the representative incident in four actions:

1. create a normal paid and confirmed pickup order;
2. make the cancellation message arrive 52 seconds late while settlement and reward are posted;
3. ask the real reconciliation engine to explain the inconsistency;
4. apply the deterministic repair plan inside the sandbox.

The primary UI shows the business result directly: `settlement +9,000 KRW / reward +90P` before repair and `settlement 0 KRW / reward 0P` after repair. It also states that the original evidence is preserved.

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
