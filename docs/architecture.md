# Architecture

## Design objective

Protect the customer's pickup-time promise and the merchant's financial state when event delivery order is unreliable.

## Command/write path

1. `commitment-service` receives a hold request with an `Idempotency-Key`.
2. Redis executes one Lua admission transaction for the `(store, pickup-minute)` slot. It prunes expired provisional holds, checks the request fingerprint, and grants capacity only if the slot invariant still holds.
3. PostgreSQL stores commitment aggregate state and its outbox event in one R2DBC transaction.
4. `OutboxPublisher` delivers unpublished commitment events to `pickup.commitment.events.v1`. A duplicate publish is allowed by design; downstream processing must be idempotent.
5. Confirmation extends the Redis token through the pickup window, so a confirmed reservation does not silently disappear when the original short hold TTL expires.
6. Cancellation appends its event first and then releases the Redis capacity token. If release fails, Redis expiry remains the final cleanup boundary.

## Financial path

The Financial Ledger is intentionally a separate bounded context.

- REST ingress: `POST /api/v1/ledger/postings`
- Kafka ingress: `pickup.financial.events.v1`
- shared posting policy: settlement/reward/reversal → balanced debit/credit batch
- `financial_event_receipt` stores a stable semantic fingerprint per source event ID; an exact redelivery is a no-op, while reuse of the same ID with different money/account data is rejected
- ledger-entry uniqueness remains a database backstop, while the receipt and entries are written in one JDBC transaction

This repository does not pretend to contain a real external payment provider. The financial ingress contract is explicit so retry and compensation behavior can still be exercised.

## Investigation / reconciliation path

1. An operator or integration submits an event envelope to FastAPI; Celery exposes the same deterministic replay as an asynchronous worker path.
2. Reconciliation compares receive-order folding with canonical business-time folding (`occurred_at`).
3. When persistence mode is enabled, raw envelopes are archived in MongoDB with a unique event ID, the normalized incident is indexed in Elasticsearch under a stable digest, and a compact audit row is inserted idempotently in PostgreSQL.
4. The engine returns a deterministic repair proposal (`REVERSE_SETTLEMENT`, `REVERSE_REWARD`, `REBUILD_PROJECTION`, `RESLOT_REVIEW`, or `MANUAL_REVIEW`).
5. The optional AI reviewer can explain the evidence packet, but its output cannot add or execute repair commands.
6. Repair-command publication is represented as an AsyncAPI contract and remains an operator/integration action in this portfolio version; no automatic financial mutation is claimed.

## Why WebFlux only on the command edge

The pickup-command path coordinates Redis, R2DBC, and asynchronous publication and benefits from non-blocking I/O. The ledger path is deliberately blocking and transactional because a small, balanced financial batch benefits more from simple JDBC transaction semantics than from introducing reactive complexity everywhere.

## CQRS boundary

The command model owns invariants. Search/operator views are rebuildable projections. Reconciliation never edits an original event just to make a read model look correct; it proposes projection rebuilds or new compensating financial events.

## Temporal model

The Kafka producer contract carries `occurred_at` but deliberately does **not** claim a universal `received_at`: receipt time is local to each consumer. When a consumer/operator archives an event for reconciliation, it adds its observation time and the evidence envelope carries both values.

Every reconciliation evidence event carries:

- `event_id`: idempotency identity;
- `aggregate_id`: order/commitment identity;
- `occurred_at`: when the business fact happened;
- `received_at`: when this service observed it;
- optional `causation_id` and `correlation_id`;
- `schema_version`;
- typed payload data.

This lets a cancellation received at `12:05` still be understood as having occurred at `11:59`, before a settlement that occurred at `12:01`.
