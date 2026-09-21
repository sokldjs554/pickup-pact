# Public demo

The public demo is a **session-isolated smart-order customer demo** with a separate hidden backend console.

## Customer smart-order experience

The default route behaves like a small consumer smart-order product rather than an incident/recovery tool.

Synthetic customer and catalog data come from real demo API endpoints:

- `GET /api/demo/customer`
- `GET /api/demo/catalog`

The visitor can:

1. choose one of three synthetic nearby stores;
2. add menu items to a cart;
3. increase or decrease item quantities and see totals recalculate;
4. place an order, which calls the real session order/payment/confirm APIs;
5. follow the order through received, preparing, and pickup-ready presentation states;
6. cancel the order through a customer-facing confirmation dialog.

The cancellation UI intentionally stays simple. The demo silently reproduces a late-cancellation race behind the customer experience, runs reconciliation, applies deterministic `REVERSE_SETTLEMENT` and `REVERSE_REWARD`, and then shows only the customer-relevant outcome: the order is cancelled and payment/points are cleaned up.

The customer-facing route does not expose backend navigation or recovery terminology.

## Technical detail layer

Backend reviewers use `/?dev=1` to enter the separate operator console. That console exposes:

- manual order lifecycle operations;
- pickup-capacity revision;
- late cancellation, exact Kafka redelivery, conflicting payload, and capacity-drop labs;
- the real `services/reconciler/app/engine.py` output;
- receive-time ordering versus business-time ordering;
- deterministic repair proposals and evidence IDs;
- reversal ledger batches and the operator audit trail.

Each browser stores its own demo session ID. The Render instance keeps only in-memory synthetic demo state; it is not connected to real merchants, customers, or payment providers.

## Representative hidden late-cancellation flow

1. the customer order is held, paid, and confirmed;
2. cancellation occurs first but reaches the demo backend 52 seconds later;
3. settlement and reward are posted during that delay;
4. reconciliation detects the stale financial state;
5. `REVERSE_SETTLEMENT` and `REVERSE_REWARD` are proposed and applied;
6. the customer sees a simple cancellation-complete state;
7. the expert console preserves the ledger and audit evidence.

A projection rebuild is proposed only when receive-order folding actually differs from canonical business-time folding. It is not hard-coded into every late-cancellation case.

## Public-demo boundary

The public demo packages the real reconciliation engine but intentionally does not boot Kafka, PostgreSQL, Redis, MongoDB, Elasticsearch, both Spring services, and Celery on the free Render process. Those integrations are represented by the service code, Compose topology, contracts, infrastructure manifests, tests, and evidence artifacts in the repository.

All customer, store, menu, order, payment, and reward data in the public demo is synthetic.
