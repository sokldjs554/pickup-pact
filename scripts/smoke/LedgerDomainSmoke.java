import io.pickuppact.ledger.domain.LedgerBatch;
import io.pickuppact.ledger.domain.LedgerDirection;
import io.pickuppact.ledger.domain.LedgerEntry;
import io.pickuppact.ledger.domain.LedgerPostingPolicy;
import java.math.BigDecimal;
import java.util.List;

public final class LedgerDomainSmoke {
    public static void main(String[] args) {
        var first = LedgerPostingPolicy.settlement("evt-1", "order-1", new BigDecimal("12000.00"));
        var same = LedgerPostingPolicy.settlement("evt-1", "order-1", new BigDecimal("12000"));
        var changed = LedgerPostingPolicy.settlement("evt-1", "order-1", new BigDecimal("12001"));
        if (!first.fingerprint().equals(same.fingerprint())) throw new AssertionError("canonical amount fingerprint mismatch");
        if (first.fingerprint().equals(changed.fingerprint())) throw new AssertionError("conflicting amount fingerprint collision");

        try {
            new LedgerBatch("evt-bad", List.of(
                new LedgerEntry("evt-bad", "order-1", "a", LedgerDirection.DEBIT, BigDecimal.TEN),
                new LedgerEntry("evt-bad", "order-1", "b", LedgerDirection.CREDIT, BigDecimal.ONE)
            ));
            throw new AssertionError("unbalanced batch should be rejected");
        } catch (IllegalArgumentException expected) {
            // expected
        }
    }
}
