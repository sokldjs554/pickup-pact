# Architecture

## Admission trust boundary

Customer-supplied workload numbers are not authoritative.

1. `POST /api/v1/commitments/quotes` receives store ID and menu SKU/quantity.
2. `MenuWorkloadPolicy` calculates preparation units and order amount on the server.
3. `QuoteTokenService` signs store, workload, amount and expiry with HMAC-SHA256.
4. `POST /hold` requires the signed quote plus `Idempotency-Key`; the client cannot lower the capacity cost by editing a request field.
5. PostgreSQL has a unique idempotency index. Concurrent retries that race after the initial lookup release their speculative Redis lease and converge on the already-created commitment.
6. Redis applies a default capacity with optional per-store overrides before executing the atomic Lua admission.

The public FastAPI demo mirrors the trust boundary: the customer browser submits structured line items, and the demo server recalculates amount/workload instead of trusting client-provided totals.

## Core Pact and financial side effects

`confirm` persists `CommitmentConfirmed` and `PickupPactIssued` together with the aggregate. Reschedule persists the new slot and `PickupPactRenegotiated`; breach persists `PickupPactBreached`.

The outbox relay publishes the domain event first and then derives deterministic financial events where the business transition has a financial consequence:

- `PickupPactBreached` → `REWARD` using `<outbox-id>-pact-reward`;
- `PickupClaimed` → `SETTLEMENT` using `<outbox-id>-settlement`.

The row is marked published only after both sends complete. If a retry republishes either message, the downstream ledger's event-ID idempotency prevents a second posting.

## Runtime readiness boundary

The reconciler now separates process liveness from dependency readiness.

- `GET /health` is a liveness signal only.
- `GET /ready` checks PostgreSQL, MongoDB, and Elasticsearch when persisted reconciliation is enabled.
- Kubernetes uses `/ready` for readiness and keeps `/health` for liveness.
- Full-topology integration waits for `/ready` before sending persisted reconciliation traffic.
- Elasticsearch indexing uses a deterministic document ID plus bounded timeout retries, so retrying a reconciliation request cannot create a second incident document.

This boundary was added after the release gate reproduced a cold-start race: the FastAPI process was healthy while Elasticsearch was still warming, causing the first persisted reconciliation to fail. The fix gates traffic on dependency readiness instead of hiding the race with a fixed sleep.

## Design objective

Protect the customer's pickup-time promise and the merchant's financial state when event delivery order is unreliable.

## Command/write path

1. `commitment-service` creates a `HELD` commitment only after Redis atomically leases capacity for the requested pickup slot.
2. The hold starts with `paymentAuthorized=false`; the caller cannot manufacture a paid state in the hold request.
3. A separate `/{id}/authorize-payment` command records the result of an external payment authorization.
4. Confirmation checks both aggregate state and payment authorization before changing the commitment to `CONFIRMED`. The current state can be read with `GET /api/v1/commitments/{id}`.
5. PostgreSQL stores commitment state and domain-outbox rows through the R2DBC repository.
6. `OutboxRelay` publishes unpublished rows to `pickup.commitment.events.v1`; downstream consumers remain idempotent because duplicate publication is possible.
7. If PostgreSQL persistence fails after Redis admission, the hold path compensates by releasing the lease. Cancellation also releases the lease after its event is committed. Redis TTL remains a final safety boundary if cleanup cannot complete.

### Capacity scope

The current Lua implementation is a per-slot atomic counter with release and TTL. The TTL covers the requested pickup time plus a grace period, which favors promise safety over early capacity reclamation. It proves the no-oversubscription invariant under the synthetic race benchmark. It does not claim token-level retry deduplication or a production abandoned-checkout timeout.

## Financial path

The Financial Ledger is a separate bounded context.

- REST ingress: `POST /api/v1/ledger/postings`
- Kafka ingress: `pickup.financial.events.v1`
- supported posting types: settlement, settlement reversal, reward, reward reversal
- shared application service and `LedgerPostingPolicy` are used by both ingress paths
- `ledger_batches.event_id` + `semantic_fingerprint` distinguish an exact redelivery from a conflicting reused event ID
- each accepted batch is balanced before repository append\n- conflicting reused IDs are recorded in `ledger_conflicts` and can be inspected through `GET /api/v1/ledger/conflicts`\n- `GET /api/v1/ledger/orders/{aggregateId}` exposes append-only financial history without mutating the ledger

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

The command model owns invariants. `POST /api/v1/projections/rebuild` explicitly upserts the canonical snapshot into PostgreSQL `commitment_projection`, and `GET /api/v1/projections/{aggregateId}` exposes that read model. Reconciliation never edits an original event merely to make a projection look correct.

## Temporal model

A business fact and its delivery are different timestamps:

- `occurred_at`: when the business fact actually happened;
- `received_at`: when a particular consumer observed it.

This distinction is why a cancellation received at 12:15:02 can still be understood as having happened at 12:14:10, before settlement at 12:14:36.
