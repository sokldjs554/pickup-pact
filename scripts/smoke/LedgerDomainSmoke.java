import io.pickuppact.ledger.domain.LedgerBatch;
import io.pickuppact.ledger.domain.LedgerDirection;
import io.pickuppact.ledger.domain.LedgerEntry;
import io.pickuppact.ledger.domain.LedgerPostingPolicy;
import java.math.BigDecimal;
import java.time.Instant;
import java.util.List;

public final class LedgerDomainSmoke {
    public static void main(String[] args) {
        var first = LedgerPostingPolicy.settlement("evt-1", "order-1", new BigDecimal("12000.00"));
        var same = LedgerPostingPolicy.settlement("evt-1", "order-1", new BigDecimal("12000"));
        var changed = LedgerPostingPolicy.settlement("evt-1", "order-1", new BigDecimal("12001"));
        if (!first.semanticFingerprint().equals(same.semanticFingerprint())) {
            throw new AssertionError("canonical amount fingerprint mismatch");
        }
        if (first.semanticFingerprint().equals(changed.semanticFingerprint())) {
            throw new AssertionError("conflicting amount fingerprint collision");
        }

        var reverse = LedgerPostingPolicy.reverseSettlement(
                "reverse-1", "order-1", new BigDecimal("3000"), "evt-1"
        );
        if (!"evt-1".equals(reverse.sourceEventId())) {
            throw new AssertionError("reversal source identity missing");
        }

        try {
            new LedgerBatch(
                    "evt-bad",
                    "fingerprint",
                    "order-1",
                    "TEST",
                    null,
                    List.of(
                            new LedgerEntry("a", LedgerDirection.DEBIT, BigDecimal.TEN, "KRW", Instant.EPOCH),
                            new LedgerEntry("b", LedgerDirection.CREDIT, BigDecimal.ONE, "KRW", Instant.EPOCH)
                    )
            );
            throw new AssertionError("unbalanced batch should be rejected");
        } catch (IllegalArgumentException expected) {
            // expected
        }
    }
}
