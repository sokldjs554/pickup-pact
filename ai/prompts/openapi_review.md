# OpenAPI change review prompt

Review an OpenAPI diff for backend correctness. Return JSON with `blocking`, `warnings`, `tests`.

Check:

- idempotency for retried POST operations;
- explicit error models and status codes;
- request/response compatibility;
- pagination and stable sort semantics;
- timestamps/time zones;
- optimistic concurrency/version fields;
- unsafe ambiguity between retryable and terminal failures;
- authorization boundaries;
- whether a financial mutation can be replayed accidentally.

Do not claim a vulnerability without pointing to a concrete operation/path in the diff.
