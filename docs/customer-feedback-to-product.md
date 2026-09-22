# Customer feedback → product decisions

This document records why the public demo adds customer-facing trust features instead of only showcasing backend patterns.

## Public signal used

On 2026-08-13, one verified Google Play reviewer of PassOrder described several concrete frustrations in a single review: payment/re-order confusion, pickup timing that did not feel meaningful, another customer taking an identical item, and concern about personal information visible on printed order slips.

This is one public review, not evidence that every user experiences these problems. It is used as a product-design signal, not as a claim about PassOrder's overall quality.

Source checked 2026-09-22: https://play.google.com/store/apps/details?hl=ko&id=com.paytalab.mkseo.passorder

## Product responses in Pickup Pact

| Customer-facing concern | Pickup Pact response | Backend evidence |
|---|---|---|
| Did my payment/order really finish? | Trust Receipt shows order, payment, time changes, cancellation cleanup, final charge and reward result in one mobile receipt | append-only event timeline + ledger/audit |
| Why was it made too early / why am I still waiting? | Pickup Promise proposes a new pickup time when synthetic capacity falls and records customer acceptance | CapacityRevised → RESLOT_REVIEW → PickupRescheduled |
| Someone else picked up the same item | One-time Pickup Code is consumed once and rejects reuse | PickupClaimed + one-time code validation + 409 on reuse |
| Why expose my phone/name on the slip? | Customer receipt uses order number and one-time pickup code; it does not display phone/name | receipt contract contains no phone/name field |

## Design principle

The customer should not operate reconciliation, ledger reversal, CQRS rebuilds, or duplicate-event handling.

The public product layer shows only the next useful customer action. The developer layer keeps the technical evidence separately available through /?dev=1.