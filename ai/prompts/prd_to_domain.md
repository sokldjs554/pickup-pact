# PRD → domain review prompt

You are reviewing a backend PRD before implementation.

Return JSON with:

- `actors`
- `business_invariants`
- `bounded_contexts`
- `commands`
- `events`
- `failure_modes`
- `idempotency_boundaries`
- `consistency_requirements`
- `open_questions`

Rules:

1. Separate business facts from implementation choices.
2. Flag any requirement that depends on last-write-wins for payment, settlement, reward, or capacity.
3. Identify which operations require strong local consistency and which can be eventually consistent.
4. Do not invent acceptance criteria absent from the PRD; place them under `open_questions`.
5. Every proposed event must say what business fact happened and which aggregate owns it.
