package io.pickuppact.ledger.domain;

import java.math.BigDecimal;
import java.util.List;
import java.util.Objects;

public record LedgerBatch(
        String eventId,
        String semanticFingerprint,
        String aggregateId,
        String reason,
        String sourceEventId,
        List<LedgerEntry> entries
) {
    public LedgerBatch {
        Objects.requireNonNull(eventId);
        Objects.requireNonNull(semanticFingerprint);
        entries = List.copyOf(entries);
        if (entries.isEmpty()) {
            throw new IllegalArgumentException("ledger batch must contain entries");
        }
        var units = entries.stream().map(LedgerEntry::currency).distinct().toList();
        if (units.size() != 1) {
            throw new IllegalArgumentException("ledger batch cannot mix accounting units");
        }

        BigDecimal debit = entries.stream()
                .filter(e -> e.direction() == LedgerDirection.DEBIT)
                .map(LedgerEntry::amount).reduce(BigDecimal.ZERO, BigDecimal::add);
        BigDecimal credit = entries.stream()
                .filter(e -> e.direction() == LedgerDirection.CREDIT)
                .map(LedgerEntry::amount).reduce(BigDecimal.ZERO, BigDecimal::add);
        if (debit.compareTo(credit) != 0) {
            throw new IllegalArgumentException("double-entry batch must balance");
        }
    }

    public BigDecimal amount() {
        return entries.getFirst().amount();
    }

    public String unit() {
        return entries.getFirst().currency();
    }

    public boolean reversal() {
        return reason.equals("REVERSE_SETTLEMENT") || reason.equals("REVERSE_REWARD");
    }
}
