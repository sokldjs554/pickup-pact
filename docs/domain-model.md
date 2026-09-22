# Domain model

## Pickup Commitment aggregate

Implemented states are `HELD`, `CONFIRMED`, `PICKED_UP`, `CANCELLED`, and `AT_RISK`.

The implemented aggregate stores:

- commitment ID;
- store ID and pickup time;
- capacity units and the Redis lease token;
- whether a payment authorization has been attached;
- current commitment state;
- optimistic version.

Implemented command flow:

0. `POST /api/v1/commitments/quotes` accepts store + menu SKU/quantity, computes preparation workload **on the server**, signs a short-lived quote token, and returns feasible 5-minute slots.
1. `POST /api/v1/commitments/hold` accepts only `quoteToken + pickupAt` plus `Idempotency-Key`; it never trusts a client-supplied capacity number.
2. `HoldCommand` verifies the quote, atomically leases capacity, and creates one `HELD` commitment. Retrying the same idempotency key and fingerprint returns the same commitment without reserving capacity again.
3. `authorizePayment` records an external payment-authorization result and emits `PaymentAuthorized`.
4. `confirm` is rejected unless payment authorization exists and pickup time is still in the future; confirmation persists both `CommitmentConfirmed` and `PickupPactIssued` in one database transaction.
5. `reschedule` reserves the replacement slot first, persists `PickupRescheduled + PickupPactRenegotiated`, then releases the old lease. A failed old-lease release is conservative under-admission until TTL, never overbooking.
6. `breach-pact` persists `PickupPactBreached`; the outbox relay derives a deterministic REWARD posting to the financial Kafka topic.
7. `claimPickup` appends `PickupClaimed`, moves the aggregate to `PICKED_UP`, and releases the lease.
8. `cancel` appends `CommitmentCancelled`, marks an active Pact cancelled, and releases capacity.
9. Reconciliation can mark a previously confirmed promise `AT_RISK` when later capacity evidence invalidates the promise.

State-transition violations use HTTP 409 semantics. Malformed/expired quote inputs use HTTP 400.

## Redis capacity model

The Redis adapter treats future preparation capacity as a **5-minute reservable resource per store**.

- Customer admission starts from `POST /api/v1/commitments/quotes`; the server maps menu SKU + quantity to capacity units before exposing feasible slots.
- `GET /api/v1/commitments/slots` remains a low-level diagnostic endpoint rather than the trusted customer admission contract.
- Menu/order workload is expressed as capacity units rather than assuming every item costs the same preparation effort.
- Admission runs in Lua: read the current slot usage, reject `used + requested > capacity`, otherwise `INCRBY` atomically.
- A successful admission creates both the slot counter and a unique `pickup:lease:<uuid>` key containing the leased units.
- The opaque lease token references the slot key and the lease key.
- Release Lua first checks the lease key. If it is already gone, release is a no-op; otherwise it deletes that identity and decrements the slot exactly once.
- The slot TTL extends through pickup plus a grace period, so a confirmed promise cannot silently lose capacity before handoff.

This matters when a request crosses storage boundaries. If the database transition to `CANCELLED` or `PICKED_UP` commits but Redis release fails, a retry sees the already-terminal commitment and retries only the idempotent release instead of emitting the domain event again. If PostgreSQL persistence fails while creating a hold, the application compensates the Redis admission.

## Financial Ledger context

The financial model is append-only. Each settlement, reward, or reversal becomes a balanced debit/credit `LedgerBatch`.

Settlement batches are denominated in `KRW`; reward/reward-reversal batches are denominated in `PTS`. A batch cannot mix accounting units, so “500 reward points” is never silently represented as 500 KRW.

`ledger_batches.event_id` is the idempotency identity and `semantic_fingerprint` is a SHA-256 digest of aggregate, posting type, normalized amount, and accounting unit.

- same event ID + same fingerprint → `DUPLICATE_NOOP`;
- same event ID + different fingerprint → `CONFLICTING_EVENT_ID`, with the existing/incoming fingerprints quarantined in `ledger_conflicts`;
- a new event → append one balanced batch and its entries.

REST (`POST /api/v1/ledger/postings`) and Kafka (`pickup.financial.events.v1`) both call the same application service and posting policy.

## Reconciliation policy

AI never decides ledger mutations. The deterministic policy maps proven anomalies to allowed repair proposals:

- duplicate event → `NO_OP_DUPLICATE`;
- receive-order projection drift → `REBUILD_PROJECTION`;
- cancellation occurred before settlement → `REVERSE_SETTLEMENT`;
- cancellation occurred before reward → `REVERSE_REWARD`;
- capacity revision invalidates a future promise → `RESLOT_REVIEW`;
- conflicting duplicate or invalid causal state → `MANUAL_REVIEW`.


### Ledger history and conflict evidence

The ledger service exposes read-only operational queries in addition to posting:

- `GET /api/v1/ledger/orders/{aggregateId}` returns append-only batch history for one order.
- `GET /api/v1/ledger/conflicts` returns quarantined reused-event-ID conflicts.

The Kafka consumer acknowledges a conflicting event only after the conflict evidence is persisted. It does not append a second financial batch.


## CQRS projection

The command contexts keep business invariants, while the reconciler owns an explicit rebuildable read model.

- `POST /api/v1/projections/rebuild` folds one aggregate's evidence into canonical event-time state and upserts `commitment_projection`.
- `GET /api/v1/projections/{aggregateId}` returns the latest rebuilt snapshot.
- The projection stores a SHA-256 `canonical_hash` so an operator can compare rebuild results without mutating the original event evidence.
- Projection rebuild is non-financial. Settlement/reward compensation still goes through the ledger boundary.
