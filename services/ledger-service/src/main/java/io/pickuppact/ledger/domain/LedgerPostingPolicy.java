package io.pickuppact.ledger.domain;

import java.math.BigDecimal;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.time.Instant;
import java.util.HexFormat;
import java.util.List;

public final class LedgerPostingPolicy {
    private LedgerPostingPolicy() {}

    public static LedgerBatch settlement(String eventId, String orderId, BigDecimal amount) {
        return batch(eventId, orderId, "SETTLEMENT", amount,
                "platform-clearing", LedgerDirection.DEBIT,
                "merchant-payable", LedgerDirection.CREDIT);
    }

    public static LedgerBatch reverseSettlement(String eventId, String orderId, BigDecimal amount) {
        return batch(eventId, orderId, "REVERSE_SETTLEMENT", amount,
                "merchant-payable", LedgerDirection.DEBIT,
                "platform-clearing", LedgerDirection.CREDIT);
    }

    public static LedgerBatch reward(String eventId, String orderId, BigDecimal amount) {
        return batch(eventId, orderId, "REWARD", amount,
                "reward-expense", LedgerDirection.DEBIT,
                "customer-reward-liability", LedgerDirection.CREDIT);
    }

    public static LedgerBatch reverseReward(String eventId, String orderId, BigDecimal amount) {
        return batch(eventId, orderId, "REVERSE_REWARD", amount,
                "customer-reward-liability", LedgerDirection.DEBIT,
                "reward-expense", LedgerDirection.CREDIT);
    }

    private static LedgerBatch batch(
            String eventId,
            String aggregateId,
            String reason,
            BigDecimal amount,
            String debitAccount,
            LedgerDirection debitDirection,
            String creditAccount,
            LedgerDirection creditDirection
    ) {
        BigDecimal normalized = amount.stripTrailingZeros();
        if (normalized.signum() <= 0) throw new IllegalArgumentException("amount must be positive");
        String fingerprint = fingerprint(aggregateId, reason, normalized);
        Instant occurredAt = Instant.now();
        return new LedgerBatch(
                eventId,
                fingerprint,
                aggregateId,
                reason,
                List.of(
                        new LedgerEntry(debitAccount, debitDirection, normalized, "KRW", occurredAt),
                        new LedgerEntry(creditAccount, creditDirection, normalized, "KRW", occurredAt)
                )
        );
    }

    private static String fingerprint(String aggregateId, String reason, BigDecimal amount) {
        String canonical = aggregateId + "|" + reason + "|" + amount.toPlainString() + "|KRW";
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256")
                    .digest(canonical.getBytes(StandardCharsets.UTF_8));
            return HexFormat.of().formatHex(digest);
        } catch (NoSuchAlgorithmException exception) {
            throw new IllegalStateException("SHA-256 unavailable", exception);
        }
    }
}
