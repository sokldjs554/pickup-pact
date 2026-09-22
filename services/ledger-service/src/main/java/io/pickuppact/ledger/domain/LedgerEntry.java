package io.pickuppact.ledger.domain;

import java.math.BigDecimal;
import java.time.Instant;
import java.util.Set;

public record LedgerEntry(
        String account,
        LedgerDirection direction,
        BigDecimal amount,
        String currency,
        Instant occurredAt
) {
    private static final Set<String> SUPPORTED_UNITS = Set.of("KRW", "PTS");

    public LedgerEntry {
        if (amount.signum() <= 0) throw new IllegalArgumentException("amount must be positive");
        if (!SUPPORTED_UNITS.contains(currency)) {
            throw new IllegalArgumentException("unsupported ledger unit: " + currency);
        }
    }
}
