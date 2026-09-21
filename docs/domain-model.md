# Domain model

## Pickup Commitment aggregate

Implemented states are `HELD`, `CONFIRMED`, `CANCELLED`, and `AT_RISK`.

The implemented aggregate stores:

- commitment ID;
- store ID and pickup time;
- capacity units and the Redis lease token;
- whether a payment authorization has been attached;
- current commitment state;
- optimistic version.

Implemented command flow:

1. `HoldCommand` leases pickup capacity and creates a `HELD` commitment with `paymentAuthorized=false`.
2. `authorizePayment` records an external payment-authorization result and emits `PaymentAuthorized`.
3. `confirm` is rejected unless payment authorization exists and pickup time is still in the future.
4. `cancel` appends `CommitmentCancelled` and releases the capacity lease.
5. Reconciliation can mark a previously confirmed promise `AT_RISK` when later capacity evidence invalidates the promise.

The public API deliberately does **not** let the caller set `paymentAuthorized=true` while creating a hold.

## Redis capacity model

The current Redis adapter uses a Lua transaction per `(store, 5-minute slot)`:

- read the slot's current used units;
- reject when `used + requested > configured capacity`;
- otherwise increment the counter atomically;
- attach a TTL as a safety boundary;
- return an opaque lease token containing the slot key and units;
- cancellation uses another Lua script to decrement the leased units atomically.

This is enough to demonstrate atomic oversubscription prevention. It does **not** claim the more elaborate token-level ZSET/fingerprint lease registry that would be needed for production-grade retry deduplication and independent token expiration.

## Financial Ledger context

The financial model is append-only. Each settlement, reward, or reversal becomes a balanced debit/credit `LedgerBatch`.

`ledger_batches.event_id` is the idempotency identity and `semantic_fingerprint` is a SHA-256 digest of aggregate, posting type, normalized amount, and currency.

- same event ID + same fingerprint → `DUPLICATE_NOOP`;
- same event ID + different fingerprint → `CONFLICTING_EVENT_ID`;
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
