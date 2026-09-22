# Sprint brief — customer trust layer

## Product problem

A smart-order backend can be technically correct while the customer still wonders:

- did my order/payment really finish?
- why did the promised pickup time move?
- how do I prove the order is mine without exposing a phone number?
- after cancellation, was the charge and reward actually cleaned up?

The sprint goal is to make those questions answerable in the product while keeping reconciliation/ledger complexity out of the customer UI.

## Scope

### Customer experience

- order and cancellation history;
- mobile Trust Receipt;
- one-time 4-digit Pickup Code;
- existing Pickup Promise reschedule flow;
- terminal orders preserved when the customer starts another order.

### Backend

- PickupClaimed domain event;
- one-time pickup-code validation and reuse rejection;
- normal settlement/reward on successful pickup;
- archived receipt projection for PICKED_UP and CANCELLED orders;
- existing compensation path produces zero-charge cancelled receipts.

### Verification / performance

- unit/API tests for pickup-code reuse, receipts and history;
- Chromium E2E for completed and cancelled receipt flows;
- PostgreSQL EXPLAIN plan capture in the release gate;
- existing three-pass regression, two-pass full topology and live Render smoke remain mandatory.

## Task breakdown / estimation

These are planning-size estimates for the portfolio sprint, not historical team velocity.

| Task | Estimate |
|---|---:|
| Customer history + receipt projection/API | M |
| One-time pickup-code invariant + PickupClaimed event | M |
| Customer history/receipt/pickup UI | M |
| E2E + negative/reuse tests | M |
| PostgreSQL EXPLAIN evidence in release gate | S |
| JD/process documentation + handoff | S |

Risk-first order: backend invariant and tests → API contract → customer UI → deployed E2E → release evidence.

## Acceptance criteria

1. A confirmed order receives a 4-digit pickup code.
2. The code is shown only in the customer pickup-ready state.
3. A correct code can complete pickup exactly once; reuse returns a conflict.
4. Pickup completion emits PickupClaimed and then records settlement/reward.
5. A completed order appears in order history with a Trust Receipt.
6. A cancelled order appears in order history with final charge 0 and reward balance 0.
7. Receipt timeline preserves customer-relevant time changes and cleanup events without exposing event IDs or infrastructure terminology.
8. Starting another customer order preserves prior terminal history.
9. The normal customer route never exposes the developer console.
10. Same release commit passes all release verification paths.

## Cross-functional handoff

### PM / product

Review the problem statement, acceptance criteria, cancellation semantics and what the customer should/should not see.

### Product design

Review copy hierarchy, pickup-code visibility, receipt information density, cancelled/completed visual states and mobile viewport behavior.

### Frontend

Use the customer demo API contract for history, receipt, pickup claim and next-order transitions. Do not recreate reconciliation decisions on the client.

### Backend

Own one-time claim invariant, event ordering, settlement/reward side effects, receipt projection and archive semantics.

### DevOps / QA

Verify exact release SHA, customer E2E, duplicate pickup-code rejection, cancellation compensation, full Compose topology and PostgreSQL plan evidence.

This is a solo portfolio handoff artifact. It does not claim that a real PaytaLab PM/designer/frontend team participated.

## QA matrix

| Flow | Happy path | Negative / regression |
|---|---|---|
| Pickup | code → PICKED_UP → receipt | wrong/reused code rejected |
| Promise | capacity risk → suggestion → reschedule | customer can still cancel |
| Cancel | delayed cancel → compensation → zero receipt | reversal cannot be duplicated |
| History | terminal orders retained | new order must not erase prior history |
| Receipt | final charge/reward/timeline | no phone/name exposure |
| Dev mode | evidence remains inspectable | hidden from customer route |

## Rollout / observability

For a real rollout, customer-facing metrics would include pickup reschedule acceptance rate, pickup-code failure/reuse rate, cancellation cleanup failures and receipt-open rate. Existing Datadog artifacts demonstrate monitor configuration only; no live production monitor is claimed.

## Retrospective questions

- Did the new UX reduce ambiguity without teaching customers backend concepts?
- Did any customer convenience feature weaken a domain invariant?
- Did browser/mobile tests miss a real deployed timing condition?
- Which evidence is measured versus only a blueprint?
