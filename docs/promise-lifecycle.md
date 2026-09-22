# Promise Lifecycle Engine

## Why this is the center of Pickup Pact

Most public order-backend examples start after an order already exists: create an order, publish Kafka events, process payment, then recover with Saga/compensation if something fails.

Pickup Pact starts one step earlier:

> **Should the system accept this remote pickup order at all, and what pickup time can it responsibly promise?**

That decision is treated as a domain policy rather than a UI estimate.

## Lifecycle

### 1. Admission — before the order exists

Inputs:

- current preparation backlog;
- order size;
- store service rate;
- estimated customer travel time;
- maximum remote-order promise horizon;
- safety buffer.

Output:

- `ACCEPT` — current arrival and preparation load are aligned;
- `OFFER_LATER` — accept the order only with a later pickup promise;
- `PAUSE` — temporarily stop remote ordering instead of making a promise the store cannot keep.

The current deterministic policy is implemented in both:

- `services/commitment-service/.../PromiseAdmissionPolicy.kt`
- `demo/promise_admission.py`

Both implementations are checked against the same golden cases.

### 2. Protection — after confirmation

A promise that was safe when accepted can become unsafe later.

A capacity revision can therefore produce:

`CapacityRevised → RESLOT_REVIEW → PickupRescheduled`

The customer sees only the next useful action: a new pickup time.

### 3. Recovery — when distributed state diverges

Late cancellation, duplicate financial delivery and receive-order drift are reconstructed by business occurrence time.

Repairs remain deterministic:

- `REVERSE_SETTLEMENT`;
- `REVERSE_REWARD`;
- projection rebuild only when evidence requires it.

### 4. Proof — after the journey

The customer-facing trust layer keeps infrastructure terminology hidden:

- one-time Pickup Code;
- terminal `PickupClaimed`;
- completed/cancelled order history;
- Trust Receipt derived from the event/ledger evidence.

## Research basis

This project does not claim to reproduce any private PaytaLab algorithm.

The admission idea is informed by public operations research showing that order-ahead systems can perform poorly when remote orders are accepted and locked in without controlling congestion, and that mechanisms such as queue capping and cancellation thresholds matter.

References:

- Ke Sun, Yunan Liu, Luyi Yang, **Order Ahead for Pickup: Promise or Peril?**, Manufacturing & Service Operations Management, 2026. https://doi.org/10.1287/msom.2024.0865
- Mehdi H. Farahani, Milind Dawande, Ganesh Janakiraman, **Order Now, Pickup in 30 Minutes: Managing Queues with Static Delivery Guarantees**, Operations Research 70(4), 2022.

The implementation here is independently designed for a synthetic portfolio environment.

## Evidence boundary

The admission benchmark is a deterministic synthetic stress model. It is intended to show the policy trade-off between:

- accepting every remote order;
- offering a later promise;
- temporarily pausing remote intake.

It is not production throughput, restaurant SLA, or customer-impact evidence.
