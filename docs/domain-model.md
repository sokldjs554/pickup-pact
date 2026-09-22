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

0. `GET /api/v1/commitments/slots` reads 5-minute slot availability for a requested workload so the customer can choose a feasible pickup time.
1. `HoldCommand` leases pickup capacity and creates a `HELD` commitment with `paymentAuthorized=false`.
2. `authorizePayment` records an external payment-authorization result and emits `PaymentAuthorized`.
3. `confirm` is rejected unless payment authorization exists and pickup time is still in the future.
4. `claimPickup` appends `PickupClaimed`, moves the aggregate to `PICKED_UP`, and releases the lease.
5. `cancel` appends `CommitmentCancelled` and releases the capacity lease.
6. Reconciliation can mark a previously confirmed promise `AT_RISK` when later capacity evidence invalidates the promise.

The public API deliberately does **not** let the caller set `paymentAuthorized=true` while creating a hold.

## Redis capacity model

The Redis adapter treats future preparation capacity as a **5-minute reservable resource per store**.

- `GET /api/v1/commitments/slots` reports configured, reserved, and available units for each future bucket.
- Menu/order workload is expressed as capacity units rather than assuming every item costs the same preparation effort.
- Admission runs in Lua: read the current slot usage, reject `used + requested > capacity`, otherwise `INCRBY` atomically.
- A successful admission creates both the slot counter and a unique `pickup:lease:<uuid>` key containing the leased units.
- The opaque lease token references the slot key and the lease key.
- Release Lua first checks the lease key. If it is already gone, release is a no-op; otherwise it deletes that identity and decrements the slot exactly once.
- The slot TTL extends through pickup plus a grace period, so a confirmed promise cannot silently lose capacity before handoff.

This matters when a request crosses storage boundaries. If the database transition to `CANCELLED` or `PICKED_UP` commits but Redis release fails, a retry sees the already-terminal commitment and retries only the idempotent release instead of emitting the domain event again. If PostgreSQL persistence fails while creating a hold, the application compensates the Redis admission.

## Financial Ledger context

The financial model is append-only. Each settlement, reward, or reversal becomes a balanced debit/credit `LedgerBatch`.

`ledger_batches.event_id` is the idempotency identity and `semantic_fingerprint` is a SHA-256 digest of aggregate, posting type, normalized amount, and currency.

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
