# AI-assisted iteration log

The target role expects AI to be used across requirement analysis, design, implementation, review, documentation and automation. This log records actual iterations that changed the repository, including cases where the first AI-assisted result was rejected or corrected.

## 1. Recovery console → product shell

Initial state: the public page was technically strong but still looked like an operator console.
Decision: split the customer-facing experience from the backend console.
Result: commit dbae95cf... introduced a product-style shell; later customer feedback showed that even this still centered the recovery mechanism too much.

## 2. Product shell → real customer smart-order journey

Problem found: a visitor could understand the recovery scenario but could not naturally browse a store, add menu items, order and follow pickup status.
Decision: rebuild the default route around a virtual customer and real demo catalog/order APIs.
Result: commit 319690fc... added store selection, cart quantity changes, checkout, order status and cancellation while moving backend controls behind /?dev=1.

## 3. Live browser failure → state-based verification

Problem found by live Render E2E: the test waited for a transient 주문됐어요 label. The deployed browser could legitimately advance to preparing/ready before the assertion observed it.
Rejected approach: increase sleeps/timeouts.
Accepted approach: expose a durable customer order stage and assert the actual state transition.
Result: commit 354d1091... stabilized the deployed verification without weakening the behavioral contract.

## 4. Fixed pickup time → dynamic promise

Problem found during review: the cart said about N minutes while the created order still used a fixed 12:30 pickup time.
Decision at that stage: compute the customer pickup clock from current Korea time + store preparation time, then verify that congestion proposes exactly +5 minutes.
Result: commit f953d2a7... connected capacity risk to a customer-friendly pickup-time proposal and PickupRescheduled event. This solved the fixed-clock bug but still let the system choose the initial pickup time for the customer.

## 5. Current trust-layer iteration

Problem: order/payment/history features alone are common in public Kafka/Saga portfolio projects.
Decision: differentiate with a customer-trust layer:
- mobile Trust Receipt;
- order/cancellation history;
- one-time Pickup Code with reuse rejection;
- existing Pickup Promise;
- backend evidence kept out of the customer UI.

## Human gates used

AI-assisted changes are not accepted because they look correct. Each release candidate must pass repository guardrails, unit/API tests, Chromium E2E, full CI, repeated release-gate verification and deployed Render smoke test on the same commit.

AI remains advisory for financial repair policy; deterministic code is authoritative.

## 6. Auto-assigned pickup time → customer-selected capacity slot

Problem found by rereading the target posting: the product description explicitly emphasizes that the customer chooses the pickup time. The previous auto-computed ETA therefore weakened product fit even though the recovery logic was technically sound.

Rejected approach: keep adding ETA prediction or make the existing dynamic time look more intelligent.

Accepted approach:
- expose five-minute capacity availability before checkout;
- weight menu items by preparation capacity units;
- require the customer to choose a feasible slot;
- reserve that slot atomically at hold time;
- preserve the existing post-confirmation promise-recovery path for unexpected capacity loss.

During the same review, an AI-assisted code audit found a more serious reliability issue: the Redis release script ignored the random lease identity embedded in the token. A duplicate release could decrement the shared slot twice. The fix stores a dedicated lease key and makes release idempotent. Terminal APIs are retry-safe when the database transition commits but Redis release fails.

Verification contract: unit tests cover slot alignment/fit and release-retry semantics; Docker integration smoke checks reservation `0 → 2 → 0` and repeats cancellation without capacity underflow; browser E2E requires an explicit pickup-time selection before checkout.
