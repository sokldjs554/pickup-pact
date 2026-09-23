# Merchant Fulfillment Reliability

Research refresh: 2026-09-23.

## Why the project center changed

The original portfolio thesis over-weighted “customer-selected scheduled pickup” as a differentiator. That is not defensible after checking the actual public PassOrder product.

Public product evidence shows:

- the customer app already treats choosing a desired pickup time as a core feature;
- the merchant app centers real-time new-order reception, acceptance, preparation completion, cancellation, automatic acceptance, printer/POS integration, pickup-time and sold-out management;
- the merchant app description explicitly emphasizes maintaining the order connection even when the app is closed to reduce missed orders;
- the backend posting emphasizes order/payment/settlement/reward business logic, DDD/domain events, Kafka/CQRS/Redis, distributed consistency, performance analysis, and AI-assisted development.

Official/public references:
- Backend Developer: https://recruit.passorder.co.kr/c/XZ4WHRTjx8?back=true
- PassOrder customer app: https://play.google.com/store/apps/details?id=com.paytalab.mkseo.passorder
- PassOrder merchant app: https://play.google.com/store/apps/details?id=com.paytalab.passorderboss
- PassOrder Business: https://biz.passorder.co.kr/

These sources describe public product behavior only. This repository does **not** claim to know or reproduce Paytalab's private architecture.

## Selected problem

**A customer-selected pickup time is only useful if the order is delivered to the merchant reliably and executed at the right preparation time.**

The merchant fulfillment context therefore protects five boundaries:

1. **Durable merchant intake** — a confirmed order becomes a durable merchant delivery.
2. **Reconnect / redelivery** — an unacknowledged delivery is replayable after reconnect, while the same Kafka event does not create duplicate POS-print or notification intent records.
3. **JIT preparation** — pickup time + preparation workload produce an earliest start, target ready, and latest guaranteed ready time.
4. **Race handling** — cancellation or pickup-time changes after preparation starts are not silently applied; they become explicit review evidence.
5. **Promise feedback** — a late READY event crosses the fulfillment event boundary and causes the existing Pickup Pact compensation flow exactly once.

## Domain flow

```text
Payment authorized
  → CommitmentConfirmed
  → merchant inbox dedupe
  → durable ORDER_AVAILABLE delivery
  → merchant reconnect / ACK
  → ACCEPTED
  → JIT preparation window opens
  → PREPARING
  → READY
       ├─ EARLY   → READY_TOO_EARLY
       ├─ ON_TIME → normal handoff
       └─ LATE    → READY_LATE
                    → fulfillment outbox
                    → Kafka
                    → PickupPactBreached
                    → 500 PTS reward
```

## Exactly-once effect boundary

Kafka itself is at-least-once. The project therefore does not claim “exactly-once Kafka delivery.”

Instead:

- `merchant_inbox_events.event_id` collapses redelivery at the application boundary;
- `merchant_effects unique(order_id, effect_type)` prevents a repeated order event from creating a second POS-print or new-order-notification intent record;
- `merchant_deliveries` stays pending until merchant ACK;
- reconnect reads unacknowledged delivery rows in monotonic sequence order;
- each fulfillment outbox scan orders visible rows by sequence before Kafka publication; this is not a global multi-relay ordering guarantee.

This is a **deduplicated database-intent** claim. Physical printing, external notification delivery, and exactly-once transport are not implemented or proven by this constraint.

## JIT preparation policy

For the portfolio model:

- preparation duration is derived from capacity workload;
- target READY is one minute before customer pickup;
- the start window opens with workload-dependent preparation time plus slack;
- READY substantially before the freshness window is `READY_TOO_EARLY`;
- READY after the Pickup Pact guarantee deadline is `READY_LATE`.

The policy values are portfolio assumptions for deterministic testing. They are not claimed to be PassOrder production rules.

## Cancellation and reschedule races

Before preparation:

- cancellation can transition automatically to `CANCELLED`;
- an accepted pickup-time change recalculates the JIT window.

After preparation begins:

- cancellation becomes `CANCELLATION_REVIEW`;
- pickup-time changes produce `RESCHEDULE_AFTER_PREPARATION`.

The point is not to guess Paytalab's actual policy. It is to show that irreversible real-world work changes the consistency boundary and should not be hidden by last-write-wins state updates.

## Evidence

The full Docker topology verifies:

- commitment event → merchant order intake;
- exact event-ID Kafka redelivery does not duplicate POS/notification intent rows;
- ACK-before/after reconnect behavior;
- start-before-window rejection;
- EARLY READY anomaly;
- reschedule-after-preparation review;
- cancellation-after-preparation review;
- READY_LATE → fulfillment event → Pickup Pact compensation → 500 PTS ledger posting.

The public Render demo mirrors the same domain decisions in a session-isolated sandbox so a reviewer can operate the flow without requiring the whole Kafka/PostgreSQL topology on the public instance.


## Verification boundary correction — 2026-09-23

Merchant `POS_PRINT` and notification effects in this repository are durable **intent records**, not a physical printer driver or an external notification provider. The tested unique constraint prevents duplicate intent rows; exactly-once physical printing/delivery is not claimed. The repair workbench is a separate, bounded synthetic approval journal. It does not resolve the authority race between cancellation approval and physical preparation, execute real refunds, or implement partial-refund allocation. The current README and repair-workbench document are the scope reference; earlier implementation-history descriptions are not a broader completion claim.
