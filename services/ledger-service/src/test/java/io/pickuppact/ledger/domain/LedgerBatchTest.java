package io.pickuppact.ledger.domain;

import org.junit.jupiter.api.Test;
import java.math.BigDecimal;
import java.time.Instant;
import java.util.List;
import static org.junit.jupiter.api.Assertions.assertThrows;

class LedgerBatchTest {
    @Test
    void unbalancedBatchIsRejected() {
        var now = Instant.now();
        assertThrows(IllegalArgumentException.class, () -> new LedgerBatch(
                "e1", "fp", "order-1", "test",
                List.of(
                        new LedgerEntry("a", LedgerDirection.DEBIT, new BigDecimal("1000"), "KRW", now),
                        new LedgerEntry("b", LedgerDirection.CREDIT, new BigDecimal("900"), "KRW", now)
                )
        ));
    }
}
