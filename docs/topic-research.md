# Topic and demo research

Research dates: 2026-09-18, refreshed 2026-09-21 and 2026-09-22.

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

## Selected technical gap

**Promise Lifecycle for remote pickup ordering.**

The project no longer starts after an order already exists. It asks two related questions:

1. **Admission:** should the system accept this remote pickup order now, and what pickup time can it responsibly promise?
2. **Integrity:** after a promise is confirmed, what happens when capacity, payment, settlement, rewards, or asynchronous facts change?

The central mechanisms are:

- deterministic `ACCEPT / OFFER_LATER / PAUSE` admission before order creation;
- customer-travel-aware pickup quoting;
- explicit remote-order queue capping instead of accept-all behavior;
- `occurred_at` vs `received_at` temporal reconciliation after confirmation;
- atomic pickup-slot capacity protection;
- idempotent financial posting semantics;
- canonical replay and late-cancellation compensation;
- one-time pickup handoff and customer-facing Trust Receipt;
- deterministic repair policy with AI kept advisory-only.

A September 2026 GitHub keyword sweep for pickup-specific admission control did not readily surface a comparable public coffee/order portfolio implementation. That is **not proof of uniqueness**, but it reinforced the choice to avoid generic food-ordering/Saga topics.

Public operations-research references also make the pre-order decision technically meaningful rather than decorative:

- Ke Sun, Yunan Liu, Luyi Yang, *Order Ahead for Pickup: Promise or Peril?* (2026), which studies the unintended effects of accepting and locking in order-ahead demand and evaluates capping/cancellation mechanisms.
- Mehdi H. Farahani, Milind Dawande, Ganesh Janakiraman, *Order Now, Pickup in 30 Minutes: Managing Queues with Static Delivery Guarantees* (2022), which studies promised pickup times and earliness/tardiness trade-offs.

## Demo redesign based on the research

The first demo version was too thin: four fixed scenario cards and a replay result. It demonstrated the reconciliation engine but did not demonstrate enough product flow.

The current demo is therefore designed as a **session-isolated smart-order operations sandbox** with six operator modules:

1. **Overview** — order, anomaly, event, ledger and quick-scenario summary.
2. **Order Flow** — create HOLD order, attach payment authorization, confirm pickup, cancel.
3. **Store Capacity** — inspect reserved/available units and publish capacity revisions.
4. **Fault Injection Lab** — inject delayed cancellation, exact Kafka redelivery, conflicting payload, and capacity drop.
5. **Reconciliation** — call the real `services/reconciler/app/engine.py`, compare receive-time vs business-time ordering, inspect anomalies and apply safe sandbox repair plans.
6. **Ledger & Audit** — inspect settlement/reward/reversal batches and every operator action.

This borrows the **product completeness expectation** of finished admin/order systems (order lifecycle, operator dashboard, visible status, auditability) while keeping Pickup Pact's technical subject uncommon.

## Evidence boundary

The public Render demo runs a compact FastAPI process and in-memory per-session sandbox for reviewer convenience. It does not claim that Kafka, PostgreSQL, Redis, MongoDB and Elasticsearch are all running on the free public instance.

The real repository still contains the service implementations, contracts, Docker Compose topology, Kubernetes manifests, Terraform blueprint, benchmarks and tests. Demo anomaly/repair calculation calls the same deterministic reconciliation engine used by the service code.
