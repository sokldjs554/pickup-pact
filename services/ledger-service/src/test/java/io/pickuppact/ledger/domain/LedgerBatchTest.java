package io.pickuppact.ledger.domain;

import org.junit.jupiter.api.Test;
import java.math.BigDecimal;
import java.time.Instant;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

class LedgerBatchTest {
    @Test
    void unbalancedBatchIsRejected() {
        var now = Instant.now();
        assertThrows(IllegalArgumentException.class, () -> new LedgerBatch(
                "e1", "fp", "order-1", "test", null,
                List.of(
                        new LedgerEntry("a", LedgerDirection.DEBIT, new BigDecimal("1000"), "KRW", now),
                        new LedgerEntry("b", LedgerDirection.CREDIT, new BigDecimal("900"), "KRW", now)
                )
        ));
    }

    @Test
    void oneBatchCannotMixMoneyAndPoints() {
        var now = Instant.now();
        assertThrows(IllegalArgumentException.class, () -> new LedgerBatch(
                "e2", "fp", "order-1", "mixed", null,
                List.of(
                        new LedgerEntry("a", LedgerDirection.DEBIT, new BigDecimal("500"), "PTS", now),
                        new LedgerEntry("b", LedgerDirection.CREDIT, new BigDecimal("500"), "KRW", now)
                )
        ));
    }

    @Test
    void rewardPolicyUsesPointsAndSettlementUsesKrw() {
        var reward = LedgerPostingPolicy.reward("reward-1", "order-1", new BigDecimal("500"));
        var settlement = LedgerPostingPolicy.settlement("settlement-1", "order-1", new BigDecimal("9000"));

        assertEquals(List.of("PTS", "PTS"), reward.entries().stream().map(LedgerEntry::currency).toList());
        assertEquals(List.of("KRW", "KRW"), settlement.entries().stream().map(LedgerEntry::currency).toList());
    }

    @Test
    void reversalRequiresAndFingerprintsSourcePostingIdentity() {
        assertThrows(
                IllegalArgumentException.class,
                () -> LedgerPostingPolicy.reverseSettlement(
                        "reverse-1", "order-1", new BigDecimal("3000"), null
                )
        );

        var first = LedgerPostingPolicy.reverseSettlement(
                "reverse-1", "order-1", new BigDecimal("3000"), "settle-1"
        );
        var differentSource = LedgerPostingPolicy.reverseSettlement(
                "reverse-1", "order-1", new BigDecimal("3000"), "settle-2"
        );

        assertEquals("settle-1", first.sourceEventId());
        org.junit.jupiter.api.Assertions.assertNotEquals(
                first.semanticFingerprint(),
                differentSource.semanticFingerprint()
        );
    }
}
