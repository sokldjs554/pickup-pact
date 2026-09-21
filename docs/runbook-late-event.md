# Runbook: late cancellation after settlement

1. Find the aggregate/correlation ID in the searchable incident timeline.
2. Confirm `CommitmentCancelled.occurred_at < SettlementPosted.occurred_at`; do not infer business order from Kafka receive time alone.
3. Verify the relevant event IDs exist in the raw MongoDB archive when persistence mode is enabled.
4. Run `/api/v1/reconcile` or `/api/v1/replay` with the bounded evidence envelope.
5. Expected deterministic repair proposal: `REVERSE_SETTLEMENT`; add `REVERSE_REWARD` only when reward evidence is present after the cancellation.
6. Treat the returned repair list as a command proposal. In the current portfolio implementation, an operator/integration must publish new repair events through the documented AsyncAPI contract; the AI review cannot publish them.
7. After compensation, rebuild or refresh the affected CQRS read model and compare the canonical state/hash.
8. Check Kafka consumer lag and Datadog/Elastic APM traces for the original delivery delay or retry source.
9. Record the root cause, blast radius, evidence IDs, and prevention action in the incident record.

## Safety rule

Never update or delete the original financial ledger rows to make the projection look correct. Corrections are new, balanced reversal entries linked to new source event IDs.
