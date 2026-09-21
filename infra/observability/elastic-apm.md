# Elastic APM mapping

Services use a shared trace vocabulary so an incident can be followed across the MSA boundary.

- `pickup-pact-commitment`: hold/confirm/cancel and outbox write
- `pickup-pact-ledger`: idempotency lookup and balanced ledger append
- `pickup-pact-reconciler`: canonical replay and repair planning
- `pickup-pact-replay-worker`: Celery replay execution

Required fields: `trace.id`, `transaction.id`, `event_id`, `aggregate_id`, `store_id`, `occurred_at`, `received_at`.
PII and raw customer text are intentionally excluded from trace labels.

Production wiring is configuration-driven through `ELASTIC_APM_SERVER_URL`, `ELASTIC_APM_SERVICE_NAME` and `ELASTIC_APM_ENVIRONMENT`. This repository contains the instrumentation contract but does not claim a live external APM account.
