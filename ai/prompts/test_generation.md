# Invariant-driven test generation prompt

Given a domain invariant and current tests, propose the smallest missing negative tests.

Prioritize:

- duplicate delivery;
- out-of-order delivery;
- expired lease/payment authorization;
- capacity race;
- retry with same idempotency key but different body;
- late cancellation after settlement/reward;
- projection rebuild from canonical events;
- poisoned/conflicting duplicate event ID.

Each proposal must include setup, action, deterministic assertion, and why an existing test does not already cover it.
