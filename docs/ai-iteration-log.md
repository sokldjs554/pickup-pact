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


## 7. Green unit tests → failed full topology → readiness boundary

The first full release-gate run after the scheduled-pickup work passed unit tests, browser E2E and repeated evidence checks but failed the persisted Docker topology. The reconciler process had already returned `/health=200`, while Elasticsearch was still warming; synchronous incident indexing timed out and made `POST /api/v1/reconcile` return 500.

Rejected approach: add an arbitrary startup sleep to the integration test.

Accepted approach:
- keep `/health` as liveness;
- add `/ready` that verifies PostgreSQL, MongoDB and Elasticsearch for persisted mode;
- make Kubernetes readiness use `/ready`;
- make topology smoke require HTTP 200 from `/ready`;
- add bounded Elasticsearch timeout retries with deterministic document IDs.

This iteration is deliberately kept in the history because the full-system failure was only visible after running the actual service topology, not from isolated tests.


## 8. Green release → second-pass contract and trust-boundary audit

A full green release was deliberately re-audited against the target Backend Developer posting instead of treating CI success as proof that the design story matched the core implementation.

Findings:
- OpenAPI described state conflicts as 409 while core `require()` paths returned 400.
- the public demo and a Kotlin `PickupPact` policy existed, but core confirmation did not persist `PickupPactIssued`;
- capacity units were caller-supplied at the core HOLD boundary;
- the topology tested the financial Kafka consumer with directly injected events instead of proving that a real commitment transition produced the financial message;
- the JD audit grouped promotional “event” logic together with implemented order/payment/settlement/reward domains.

Corrections:
- state invariants now throw state conflicts and full-topology HTTP tests assert 409;
- confirmation/reschedule/breach persist the Pact lifecycle in the aggregate and outbox;
- server-side SKU policy computes workload and amount, a signed short-lived quote crosses the client boundary, and HOLD requires an idempotency key;
- Pact breach and pickup claim derive REWARD/SETTLEMENT messages through the real outbox → Kafka → ledger path;
- concurrent same-key HOLD requests are replayed in the full topology;
- the JD audit explicitly says a separate promotion/coupon campaign bounded context is **not** claimed.

This iteration is useful evidence for the posting's “analyze AI output and repeatedly improve it” requirement: a previously green, polished result was not accepted until contract, trust, and evidence boundaries matched the implementation.


## 9. Automatic compensation → deadline-enforced compensation

A second domain audit found that the core `breach-pact` command could grant the 500P guarantee reward immediately after confirmation because the aggregate checked Pact status but not the guarantee deadline.

Rejected result: treating “active Pact” as sufficient proof that compensation is due.

Correction:
- `PickupPact.breach(observedAt)` now rejects any breach before `latestAt`;
- core service evaluates the invariant with server time;
- unit tests distinguish early 409 from overdue compensation;
- full-topology integration first proves early breach is rejected, then moves only the persisted test fixture deadline into the past and verifies the real outbox → Kafka → 500 PTS ledger path.

The correction prevents callers from turning a future guarantee into an immediate reward while keeping the financial side effect deterministic and idempotent.
