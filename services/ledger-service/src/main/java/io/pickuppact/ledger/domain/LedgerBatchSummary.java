package io.pickuppact.ledger.domain;

import java.math.BigDecimal;
import java.time.Instant;

public record LedgerBatchSummary(
        String eventId,
        String aggregateId,
        String reason,
        BigDecimal amount,
        String unit,
        Instant createdAt
) {}
