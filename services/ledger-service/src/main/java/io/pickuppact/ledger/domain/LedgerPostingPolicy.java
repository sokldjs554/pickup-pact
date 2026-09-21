package io.pickuppact.ledger.domain;

import java.math.BigDecimal;
import java.time.Instant;
import java.util.List;

public final class LedgerPostingPolicy {
    private LedgerPostingPolicy() {}

    private static String fingerprint(String type, String orderId, BigDecimal amount) {
        return type + ":" + orderId + ":" + amount.stripTrailingZeros().toPlainString();
    }

    private static LedgerEntry entry(String account, LedgerDirection direction, BigDecimal amount, Instant now) {
        return new LedgerEntry(account, direction, amount, "KRW", now);
    }

    public static LedgerBatch settlement(String eventId, String orderId, BigDecimal amount) {
        var now = Instant.now();
        return new LedgerBatch(
                eventId,
                fingerprint("SETTLEMENT", orderId, amount),
                orderId,
                "SETTLEMENT",
                List.of(
                        entry("platform-clearing", LedgerDirection.DEBIT, amount, now),
                        entry("merchant-payable", LedgerDirection.CREDIT, amount, now)
                )
        );
    }

    public static LedgerBatch reverseSettlement(String eventId, String orderId, BigDecimal amount) {
        var now = Instant.now();
        return new LedgerBatch(
                eventId,
                fingerprint("REVERSE_SETTLEMENT", orderId, amount),
                orderId,
                "REVERSE_SETTLEMENT",
                List.of(
                        entry("merchant-payable", LedgerDirection.DEBIT, amount, now),
                        entry("platform-clearing", LedgerDirection.CREDIT, amount, now)
                )
        );
    }

    public static LedgerBatch reward(String eventId, String orderId, BigDecimal amount) {
        var now = Instant.now();
        return new LedgerBatch(
                eventId,
                fingerprint("REWARD", orderId, amount),
                orderId,
                "REWARD",
                List.of(
                        entry("reward-expense", LedgerDirection.DEBIT, amount, now),
                        entry("customer-reward-liability", LedgerDirection.CREDIT, amount, now)
                )
        );
    }

    public static LedgerBatch reverseReward(String eventId, String orderId, BigDecimal amount) {
        var now = Instant.now();
        return new LedgerBatch(
                eventId,
                fingerprint("REVERSE_REWARD", orderId, amount),
                orderId,
                "REVERSE_REWARD",
                List.of(
                        entry("customer-reward-liability", LedgerDirection.DEBIT, amount, now),
                        entry("reward-expense", LedgerDirection.CREDIT, amount, now)
                )
        );
    }
}
