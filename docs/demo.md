# Interview demo

The public demo is a **session-isolated smart-order operations sandbox**, not a static architecture page.

## Reviewer flow

A reviewer can:

1. create a HELD pickup order;
2. attach payment authorization;
3. confirm the pickup promise;
4. post normal settlement and reward events;
5. inject late cancellation, exact Kafka redelivery, conflicting payload, or capacity reduction;
6. run the real `services/reconciler/app/engine.py`;
7. compare receive-time ordering with business-time ordering;
8. inspect deterministic repair proposals and evidence IDs;
9. apply supported repair plans **inside the demo sandbox only**;
10. inspect reversal ledger batches and the operator audit trail.

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
