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

    public static LedgerBatch posting(
            LedgerPostingType type,
            String eventId,
            String aggregateId,
            BigDecimal amount
    ) {
        return switch (type) {
            case SETTLEMENT -> settlement(eventId, aggregateId, amount);
            case REVERSE_SETTLEMENT -> reverseSettlement(eventId, aggregateId, amount);
            case REWARD -> reward(eventId, aggregateId, amount);
            case REVERSE_REWARD -> reverseReward(eventId, aggregateId, amount);
        };
    }

    public static LedgerBatch settlement(String eventId, String orderId, BigDecimal amount) {
        return batch(eventId, orderId, LedgerPostingType.SETTLEMENT, amount, "KRW",
                "platform-clearing", LedgerDirection.DEBIT,
                "merchant-payable", LedgerDirection.CREDIT);
    }

    public static LedgerBatch reverseSettlement(String eventId, String orderId, BigDecimal amount) {
        return batch(eventId, orderId, LedgerPostingType.REVERSE_SETTLEMENT, amount, "KRW",
                "merchant-payable", LedgerDirection.DEBIT,
                "platform-clearing", LedgerDirection.CREDIT);
    }

    public static LedgerBatch reward(String eventId, String orderId, BigDecimal amount) {
        return batch(eventId, orderId, LedgerPostingType.REWARD, amount, "PTS",
                "reward-expense", LedgerDirection.DEBIT,
                "customer-reward-liability", LedgerDirection.CREDIT);
    }

    public static LedgerBatch reverseReward(String eventId, String orderId, BigDecimal amount) {
        return batch(eventId, orderId, LedgerPostingType.REVERSE_REWARD, amount, "PTS",
                "customer-reward-liability", LedgerDirection.DEBIT,
                "reward-expense", LedgerDirection.CREDIT);
    }

    private static LedgerBatch batch(
            String eventId,
            String aggregateId,
            LedgerPostingType type,
            BigDecimal amount,
            String unit,
            String debitAccount,
            LedgerDirection debitDirection,
            String creditAccount,
            LedgerDirection creditDirection
    ) {
        BigDecimal normalized = amount.stripTrailingZeros();
        if (normalized.signum() <= 0) throw new IllegalArgumentException("amount must be positive");
        String reason = type.name();
        String fingerprint = fingerprint(aggregateId, reason, normalized, unit);
        Instant occurredAt = Instant.now();
        return new LedgerBatch(
                eventId,
                fingerprint,
                aggregateId,
                reason,
                List.of(
                        new LedgerEntry(debitAccount, debitDirection, normalized, unit, occurredAt),
                        new LedgerEntry(creditAccount, creditDirection, normalized, unit, occurredAt)
                )
        );
    }

    private static String fingerprint(
            String aggregateId,
            String reason,
            BigDecimal amount,
            String unit
    ) {
        String canonical = aggregateId + "|" + reason + "|" + amount.toPlainString() + "|" + unit;
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256")
                    .digest(canonical.getBytes(StandardCharsets.UTF_8));
            return HexFormat.of().formatHex(digest);
        } catch (NoSuchAlgorithmException exception) {
            throw new IllegalStateException("SHA-256 unavailable", exception);
        }
    }
}
