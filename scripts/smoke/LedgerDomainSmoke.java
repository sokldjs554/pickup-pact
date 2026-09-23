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

        // Persisted pre-allocation fingerprints must remain valid after an upgrade.
        if (!"8a729cd726dc0b89df52c90bf723d6be909aef1d87e4c3e240ef27a27aa48b12".equals(first.semanticFingerprint())) {
            throw new AssertionError("legacy settlement fingerprint changed; a valid retry would conflict");
        }
        var reward = LedgerPostingPolicy.reward("reward-1", "order-1", new BigDecimal("90.00"));
        if (!"0fdb5a1551e377164cbddc3ba90db63f4fe8ae0aeb6ea278d2ed2b106ff08179".equals(reward.semanticFingerprint())) {
            throw new AssertionError("legacy reward fingerprint changed; a valid retry would conflict");
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
