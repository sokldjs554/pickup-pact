# Domain model

## Pickup Commitment aggregate

Primary states: `DRAFT → HELD → CONFIRMED`, with `AT_RISK`, `REPAIRED`, `CANCELLED`, and `COMPLETED` reserved in the state model for recovery/fulfilment transitions.

Implemented value objects:

- `PickupSlot(storeId, pickupAt, capacityUnits)`
- `CapacityLease(token, expiresAt)`
- `PaymentAuthorization(authorizationId, expiresAt)`
- `promiseRevision`

Implemented commands/use cases:

- `HoldCommand` — lease store-slot capacity;
- `ConfirmCommand` — attach a still-valid payment authorization and confirm the promise;
- `CancelCommand` — append a cancellation and release the slot capacity.

The reconciliation boundary also understands cross-context evidence events such as `CapacityRevised`, `SettlementPosted`, `RewardGranted`, `SettlementReversed`, and `RewardReversed`.

## Redis capacity model

The capacity adapter does more than a single counter increment. Each `(store, pickup-minute)` uses Redis Cluster-compatible keys with the same hash tag:

- `used` counter;
- expiry `ZSET` for provisional or committed tokens;
- token → units `HASH`;
- token → request fingerprint `HASH`.

The Lua admission transaction first prunes expired provisional holds and decrements their units, then checks an idempotency token/fingerprint, then admits only if `used + requested <= capacity`. Confirmation extends the token expiry to the pickup window plus a grace period; cancellation atomically subtracts the token's units. This keeps retries and expired holds from turning a basic Redis counter into silent overbooking.

## Financial Ledger context

The financial model is append-only. Each settlement/reward/reversal produces a balanced debit/credit batch. Before the entries are appended, a `financial_event_receipt` records a stable SHA-256 fingerprint of the semantic posting. An exact redelivery of the source event ID becomes a no-op; the same event ID paired with different aggregate/account/direction/amount data is rejected instead of being silently accepted. The receipt and entries share one JDBC transaction. The same posting policy is reachable through REST and the `pickup.financial.events.v1` Kafka ingress.

## Reconciliation policy

AI never decides ledger mutations. The deterministic policy maps proven anomalies to allowed repair proposals:

- duplicate event → `NO_OP_DUPLICATE`;
- stale projection → `REBUILD_PROJECTION`;
- cancellation occurred before settlement → `REVERSE_SETTLEMENT`;
- cancellation occurred before reward → `REVERSE_REWARD`;
- capacity revision invalidates a future promise → `RESLOT_REVIEW`;
- conflicting duplicate or invalid causal state → `MANUAL_REVIEW`.
