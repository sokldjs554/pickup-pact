package io.pickuppact.ledger.domain;

import java.math.BigDecimal;
import java.time.Instant;

public record LedgerEntry(
        String account,
        LedgerDirection direction,
        BigDecimal amount,
        String currency,
        Instant occurredAt
) {
    public LedgerEntry {
        if (amount.signum() <= 0) throw new IllegalArgumentException("amount must be positive");
        if (!"KRW".equals(currency)) throw new IllegalArgumentException("demo supports KRW only");
    }
}
