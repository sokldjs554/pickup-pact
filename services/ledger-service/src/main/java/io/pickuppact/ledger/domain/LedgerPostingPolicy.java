package io.pickuppact.ledger.domain;

import java.math.BigDecimal;
import java.util.List;

public final class LedgerPostingPolicy {
    private LedgerPostingPolicy() {}

    public static LedgerBatch settlement(String eventId, String orderId, BigDecimal amount) {
        return new LedgerBatch(eventId, List.of(
                new LedgerEntry(eventId, orderId, "platform-clearing", LedgerDirection.DEBIT, amount),
                new LedgerEntry(eventId, orderId, "merchant-payable", LedgerDirection.CREDIT, amount)
        ));
    }

    public static LedgerBatch reverseSettlement(String eventId, String orderId, BigDecimal amount) {
        return new LedgerBatch(eventId, List.of(
                new LedgerEntry(eventId, orderId, "merchant-payable", LedgerDirection.DEBIT, amount),
                new LedgerEntry(eventId, orderId, "platform-clearing", LedgerDirection.CREDIT, amount)
        ));
    }

    public static LedgerBatch reward(String eventId, String orderId, BigDecimal amount) {
        return new LedgerBatch(eventId, List.of(
                new LedgerEntry(eventId, orderId, "reward-expense", LedgerDirection.DEBIT, amount),
                new LedgerEntry(eventId, orderId, "customer-reward-liability", LedgerDirection.CREDIT, amount)
        ));
    }

    public static LedgerBatch reverseReward(String eventId, String orderId, BigDecimal amount) {
        return new LedgerBatch(eventId, List.of(
                new LedgerEntry(eventId, orderId, "customer-reward-liability", LedgerDirection.DEBIT, amount),
                new LedgerEntry(eventId, orderId, "reward-expense", LedgerDirection.CREDIT, amount)
        ));
    }
}
