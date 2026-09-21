# Topic research: avoiding a common portfolio clone

Research date: 2026-09-18.

The goal was to avoid choosing a topic merely because it mirrors the job description. Public repositories already contain many near-identical food-order backends using Spring, Kafka, CQRS, Saga, and Outbox.

## Common patterns found

1. **Digital Restaurant** — Kotlin, Spring, DDD, Event Sourcing, CQRS, Kafka.
   https://github.com/idugalic/digital-restaurant
2. **food-ordering-system** — Spring, Kafka, Saga, Outbox, order/payment/restaurant services.
   https://github.com/Said-Aabilla/food-ordering-system
3. **food-ordering-system** — DDD/Hexagonal/Saga/Outbox/Kafka with order/payment/restaurant/customer.
   https://github.com/omonuj/food-ordering-system
4. **Course-derived food ordering implementation** — the same four-service Saga/Outbox/CQRS pattern is replicated publicly.
   https://github.com/agelenler/food-ordering-system
5. **CraveKart** — food delivery backend with Saga, Outbox, idempotency and real-time tracking.
   https://github.com/cyro17/craveKart
6. **Restaurant management system** — Spring Boot, Kafka, Redis, PostgreSQL, CQRS, Saga, transactional outbox.
   https://github.com/iam-ssrivastav/restaurant-management-system
7. **Event-driven e-commerce** — Kafka/CQRS/Saga/Outbox/Debezium/Redis/Elasticsearch and observability.
   https://github.com/tahaberkamcadev/ecom
8. **Coffee Shop Simulation Engine** — peak traffic, priority scheduling, barista load balancing, SLA monitoring.
   https://github.com/srinivas7075/CoffeeShop-Simulation-Engine

9. **order66** — a coffee pre-order project that explicitly says it was built with PassOrder as a reference.
   https://github.com/f-lab-edu/order66
10. **Lineweb Restaurant Orders** — includes scheduled pickup windows and server-side availability revalidation.
   https://github.com/drewmt/lineweb-restaurant-orders
11. **SMART-CANTEEN-SYSTEM** — slot-based pickup ordering to distribute kitchen load.
   https://github.com/Rare-Atom/SMART-CANTEEN-SYSTEM

## Topics rejected as too common

- direct PassOrder-style coffee pre-order clone;
- normal menu/cart/order/payment CRUD;
- generic food-delivery microservices;
- Saga/Outbox demonstration as the project's main novelty;
- real-time order tracking by itself;
- simple Redis menu caching;
- generic restaurant queue/load-balancing simulation;
- payment ledger by itself;
- AI recommendation/chatbot bolted onto a food app.

## Selected gap

**Scheduled pickup commitment integrity under temporal disorder.**

The system treats a promised pickup time as a business commitment spanning capacity, payment, loyalty and settlement. The hard case is not placing an order; it is recovering when facts arrive in a different order from when they happened.

The differentiators are:

- explicit `occurred_at` vs `received_at` semantics;
- atomic pickup-slot capacity leases;
- idempotent financial side effects;
- canonical replay and projection repair;
- compensation when a late cancellation predates settlement/reward posting;
- operator-visible evidence explaining why a repair was proposed;
- AI used in the engineering workflow, not trusted to mutate money/order state.

This still maps directly to the target domain (order, payment, settlement, reward, event-driven MSA, performance/troubleshooting) without copying the common tutorial architecture as the project's main story.
