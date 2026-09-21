package io.pickuppact.ledger.domain;

import java.time.Instant;

public record LedgerBatchSummary(
        String eventId,
        String aggregateId,
        String reason,
        Instant createdAt
) {}
