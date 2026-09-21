# Architecture

## Design objective

Protect the customer's pickup-time promise and the merchant's financial state when event delivery order is unreliable.

## Command/write path

1. `commitment-service` creates a `HELD` commitment only after Redis atomically leases capacity for the requested pickup slot.
2. The hold starts with `paymentAuthorized=false`; the caller cannot manufacture a paid state in the hold request.
3. A separate `/{id}/authorize-payment` command records the result of an external payment authorization.
4. Confirmation checks both aggregate state and payment authorization before changing the commitment to `CONFIRMED`.
5. PostgreSQL stores commitment state and domain-outbox rows through the R2DBC repository.
6. `OutboxRelay` publishes unpublished rows to `pickup.commitment.events.v1`; downstream consumers remain idempotent because duplicate publication is possible.
7. Cancellation appends a cancellation event and then releases the Redis lease. Redis TTL remains a safety boundary if cleanup cannot complete.

### Capacity scope

The current Lua implementation is a per-slot atomic counter with release and TTL. It proves the no-oversubscription invariant under the synthetic race benchmark. It does not claim token-level retry deduplication or independently expiring confirmed leases.

## Financial path

The Financial Ledger is a separate bounded context.

- REST ingress: `POST /api/v1/ledger/postings`
- Kafka ingress: `pickup.financial.events.v1`
- supported posting types: settlement, settlement reversal, reward, reward reversal
- shared application service and `LedgerPostingPolicy` are used by both ingress paths
- `ledger_batches.event_id` + `semantic_fingerprint` distinguish an exact redelivery from a conflicting reused event ID
- each accepted batch is balanced before repository append

The portfolio does not pretend to contain a real payment provider. The payment-authorization command represents the result boundary that a provider adapter would own.

## Investigation / reconciliation path

1. An operator or integration submits one aggregate's event envelope to FastAPI; Celery exposes the same deterministic replay asynchronously.
2. Reconciliation compares receive-order folding with canonical business-time folding (`occurred_at`).
3. When persistence mode is enabled:
   - MongoDB archives each distinct **delivery** using a delivery digest, so conflicting copies of one event ID are not lost;
   - Elasticsearch indexes the normalized reconciliation result under a stable digest;
   - PostgreSQL inserts an idempotent `reconciliation_run` row.
4. The engine returns only deterministic repair proposals such as `REVERSE_SETTLEMENT`, `REVERSE_REWARD`, `REBUILD_PROJECTION`, `RESLOT_REVIEW`, or `MANUAL_REVIEW`.
5. The optional AI reviewer can explain the evidence packet but cannot add or execute repair commands.
6. Repair-command publication remains an operator/integration action in this portfolio version; no automatic real-money mutation is claimed.

## Why WebFlux only on the command edge

The pickup-command path coordinates Redis, R2DBC, and asynchronous event publication and benefits from non-blocking I/O. The ledger path is deliberately blocking and transactional because a small balanced financial batch benefits from straightforward JDBC transaction semantics.

## CQRS boundary

The command model owns invariants. Search/operator views are rebuildable projections. Reconciliation never edits an original event merely to make a projection look correct.

## Temporal model

A business fact and its delivery are different timestamps:

- `occurred_at`: when the business fact actually happened;
- `received_at`: when a particular consumer observed it.

This distinction is why a cancellation received at 12:15:02 can still be understood as having happened at 12:14:10, before settlement at 12:14:36.
