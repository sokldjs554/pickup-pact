# Topic and demo research

Research dates: 2026-09-18, refreshed 2026-09-21 and 2026-09-23.

The goal is twofold:

1. avoid a generic food-ordering/Saga tutorial as the project's main technical story;
2. avoid a thin “incident card” demo that proves only one API call and does not feel like a finished product.

No source code from the projects below is copied. They are used to identify common portfolio patterns and useful product/demo expectations.

## Public projects reviewed

### PassOrder-adjacent / food-order backends

1. **order66** — explicitly describes itself as a coffee pre-order service inspired by PassOrder and includes separate screen/ERD design.
   https://github.com/f-lab-edu/order66

2. **omonuj/food-ordering-system** — runnable order/payment/restaurant/customer services with DDD, Saga, Outbox, Kafka, PostgreSQL, plus an order status flow that can be queried end to end.
   https://github.com/omonuj/food-ordering-system

3. **agelenler/food-ordering-system** — the common Spring/Kafka/Saga/Outbox/CQRS tutorial shape.
   https://github.com/agelenler/food-ordering-system

4. **Said-Aabilla/food-ordering-system** — DDD/Hexagonal/Kafka/Saga/Outbox implementation with explicit diagrams and distributed workflow documentation.
   https://github.com/Said-Aabilla/food-ordering-system

These confirmed that merely having order/payment services, Kafka, Saga, Outbox, and CQRS is not distinctive enough.

## Public product/dashboard references reviewed

5. **raouf-b-dev/ecommerce-admin-dashboard** — operator dashboard with dashboard, inventory, orders, users, analytics, OpenAPI-typed API access, unit/build/E2E verification.
   https://github.com/raouf-b-dev/ecommerce-admin-dashboard

6. **td041/restaurant-management** — customer ordering plus a real-time admin dashboard, live order status and staff/role concepts.
   https://github.com/td041/restaurant-management

7. **amariwan/restaurant-ordering-system** — full-stack restaurant product with kitchen order board, visible status progression, responsive dashboards and Playwright E2E testing.
   https://github.com/amariwan/restaurant-ordering-system

These projects changed the demo requirement: a finished-looking portfolio should let a reviewer **operate** a workflow, not only read architecture text.

## Topic rejected as too common

- direct PassOrder coffee-order CRUD clone;
- normal menu/cart/order/payment CRUD;
- generic food-delivery microservices;
- Saga/Outbox/CQRS as the novelty by itself;
- a chatbot or recommendation feature bolted onto ordering;
- Redis caching as the headline feature;
- a static architecture dashboard with no interactive state.

## Actual PassOrder product refresh

The original topic research was incomplete because it focused on public code patterns and the job posting more than the actual customer/merchant product surfaces. The 2026-09-23 refresh checked official/public PassOrder sources before changing the project thesis.

Observed public product behavior:

- the customer app already offers customer-selected pickup timing, so scheduled pickup itself is **not** a portfolio differentiator;
- the merchant app emphasizes real-time order reception, accept/preparation-complete/cancel actions, automatic acceptance, printer/POS integration, pickup-time and sold-out management, and maintaining the order connection when the app is closed;
- the backend posting explicitly asks for order/payment/settlement/reward business logic, DDD/domain events, Kafka/CQRS/Redis, distributed consistency, performance tuning, and AI-assisted iteration.

Public references:
- https://recruit.passorder.co.kr/c/XZ4WHRTjx8?back=true
- https://play.google.com/store/apps/details?id=com.paytalab.mkseo.passorder
- https://play.google.com/store/apps/details?id=com.paytalab.passorderboss
- https://biz.passorder.co.kr/

These are used to identify public product problems, **not** to infer Paytalab's private architecture.

## Selected technical gap

**Merchant order intake & fulfillment reliability behind an already customer-selected pickup time.**

The central question is no longer “can the customer reserve 12:30?” It is:

> after payment and confirmation, can the store reliably receive that order, survive reconnect/redelivery without duplicate POS/notification effects, prepare it in the right freshness window, and feed late execution back into the customer promise and financial ledger?

The headline mechanisms are therefore:

- durable merchant delivery + explicit ACK/reconnect;
- inbox dedupe for at-least-once Kafka delivery;
- exactly-once **business effects** for POS print and new-order notification;
- JIT preparation window derived from pickup time + workload;
- explicit EARLY / ON_TIME / LATE readiness;
- cancellation and reschedule review after irreversible preparation begins;
- late READY feedback into Pickup Pact compensation;
- existing capacity, temporal reconciliation, and financial ledger boundaries underneath.

## Customer promise subdomain — Pickup Pact Guarantee

Public portfolio projects reviewed above mostly differentiate with Saga, CQRS, Kafka, Outbox, DLQ or chaos tooling. Those are useful implementation patterns but are common enough that they do not create a memorable product story by themselves.

Pickup Pact instead turns the pickup promise into a **versioned customer contract**:

1. `PickupPactIssued` — checkout issues a promised pickup time, a latest guaranteed time, and automatic compensation terms;
2. `PickupPactRenegotiated` — capacity changes do not silently overwrite the promise; customer acceptance creates a new version;
3. `PickupPactBreached` — crossing the guaranteed window is explicit evidence;
4. automatic `RewardGranted` — compensation is applied without requiring a support request;
5. Trust Receipt — the issued pact, renegotiation and compensation remain visible to the customer.

A public on-time compensation example exists for food delivery (Foodpanda On-Time Promise), but its public terms explicitly exclude pickup orders. Pickup Pact applies the concept specifically to scheduled pickup and connects it to store-capacity commitments and event-time consistency.

Reference checked 2026-09-22:
https://www.foodpanda.hk/contents/on-time-promise

## Demo redesign based on the research

The first demo version was too thin: four fixed scenario cards and a replay result. It demonstrated the reconciliation engine but did not demonstrate enough product flow.

The current demo is therefore designed as a **session-isolated smart-order and merchant-fulfillment sandbox**. The default product layer includes customer ordering plus a merchant-operations page, while the hidden dev layer keeps recovery internals separate.

Product layer:
1. **Customer smart order** — store/menu/cart, signed workload quote behavior, pickup selection, Pickup Pact, pickup code and receipt.
2. **Merchant operations** — durable delivery/ACK, redelivery without duplicate effects, JIT start, EARLY/LATE READY, cancellation/reschedule review.

Hidden technical layer:
3. **Order Flow** — create HOLD order, attach payment authorization, confirm pickup, cancel.
4. **Store Capacity** — inspect reserved/available units and publish capacity revisions.
5. **Fault Injection Lab** — inject delayed cancellation, exact Kafka redelivery, conflicting payload, and capacity drop.
6. **Reconciliation** — call the real `services/reconciler/app/engine.py`, compare receive-time vs business-time ordering, inspect anomalies and apply safe sandbox repair plans.
7. **Ledger & Audit** — inspect settlement/reward/reversal batches and every operator action.

This borrows the **product completeness expectation** of finished admin/order systems (order lifecycle, operator dashboard, visible status, auditability) while keeping Pickup Pact's technical subject uncommon.

## Evidence boundary

The public Render demo runs a compact FastAPI process and in-memory per-session sandbox for reviewer convenience. It does not claim that Kafka, PostgreSQL, Redis, MongoDB and Elasticsearch are all running on the free public instance.

The real repository still contains the service implementations, contracts, Docker Compose topology, Kubernetes manifests, Terraform blueprint, benchmarks and tests. Demo anomaly/repair calculation calls the same deterministic reconciliation engine used by the service code.
