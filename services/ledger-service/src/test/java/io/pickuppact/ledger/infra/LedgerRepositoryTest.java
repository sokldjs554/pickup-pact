package io.pickuppact.ledger.infra;

import io.pickuppact.ledger.domain.LedgerBatch;
import io.pickuppact.ledger.domain.LedgerPostingPolicy;
import java.math.BigDecimal;
import java.util.Collections;
import org.junit.jupiter.api.Test;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.RowMapper;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

class LedgerRepositoryTest {
    private static final String ORDER = "order-1";
    private static final String SOURCE = "source-1";

    @Test
    void unattributedSettlementReversalBlocksFurtherSourceSpending() {
        var jdbc = source("SETTLEMENT", "KRW", "9000", "0");
        var batch = LedgerPostingPolicy.reverseSettlement("reverse-1", ORDER, BigDecimal.ONE, SOURCE);
        legacyReversal(jdbc, batch, true);

        assertFalse(new LedgerRepository(jdbc).reversalAllowed(batch));
        verify(jdbc).queryForObject(contains("select exists"), eq(Boolean.class),
                eq(ORDER), eq("REVERSE_SETTLEMENT"));
        verify(jdbc, never()).queryForObject(contains("select min(amount)"), eq(BigDecimal.class), eq(SOURCE));
    }

    @Test
    void unattributedRewardReversalBlocksFurtherSourceSpending() {
        var jdbc = source("REWARD", "PTS", "90", "0");
        var batch = LedgerPostingPolicy.reverseReward("reverse-2", ORDER, BigDecimal.ONE, SOURCE);
        legacyReversal(jdbc, batch, true);

        assertFalse(new LedgerRepository(jdbc).reversalAllowed(batch));
        verify(jdbc).queryForObject(contains("select exists"), eq(Boolean.class),
                eq(ORDER), eq("REVERSE_REWARD"));
    }

    @Test
    void unknownLegacyReversalCheckFailsClosed() {
        var jdbc = source("SETTLEMENT", "KRW", "9000", "0");
        var batch = LedgerPostingPolicy.reverseSettlement("reverse-3", ORDER, BigDecimal.ONE, SOURCE);
        legacyReversal(jdbc, batch, null);

        assertFalse(new LedgerRepository(jdbc).reversalAllowed(batch));
    }

    @Test
    void explicitlyAttributedPartialReversalStillUsesRemainingSourceBalance() {
        var jdbc = source("SETTLEMENT", "KRW", "9000", "3000");
        var batch = LedgerPostingPolicy.reverseSettlement("reverse-4", ORDER, new BigDecimal("6000"), SOURCE);
        legacyReversal(jdbc, batch, false);

        assertTrue(new LedgerRepository(jdbc).reversalAllowed(batch));
    }

    @Test
    void explicitlyAttributedRewardReversalStillUsesRemainingSourceBalance() {
        var jdbc = source("REWARD", "PTS", "90", "30");
        var batch = LedgerPostingPolicy.reverseReward("reverse-5", ORDER, new BigDecimal("60"), SOURCE);
        legacyReversal(jdbc, batch, false);

        assertTrue(new LedgerRepository(jdbc).reversalAllowed(batch));
    }

    @Test
    void oneUnitAboveRemainingSourceBalanceIsRejected() {
        var jdbc = source("SETTLEMENT", "KRW", "9000", "3000");
        var batch = LedgerPostingPolicy.reverseSettlement("reverse-6", ORDER, new BigDecimal("6001"), SOURCE);
        legacyReversal(jdbc, batch, false);

        assertFalse(new LedgerRepository(jdbc).reversalAllowed(batch));
    }

    private JdbcTemplate source(String reason, String unit, String amount, String reversed) {
        var jdbc = mock(JdbcTemplate.class);
        when(jdbc.query(contains("for update"), org.mockito.ArgumentMatchers.<RowMapper<String[]>>any(), eq(SOURCE)))
                .thenReturn(Collections.singletonList(new String[]{ORDER, reason}));
        when(jdbc.queryForObject(contains("select min(amount)"), eq(BigDecimal.class), eq(SOURCE)))
                .thenReturn(new BigDecimal(amount));
        when(jdbc.queryForObject(contains("select min(currency)"), eq(String.class), eq(SOURCE)))
                .thenReturn(unit);
        when(jdbc.queryForObject(contains("select coalesce(sum(amount)"), eq(BigDecimal.class), eq(SOURCE), anyString()))
                .thenReturn(new BigDecimal(reversed));
        return jdbc;
    }

    private void legacyReversal(JdbcTemplate jdbc, LedgerBatch batch, Boolean exists) {
        when(jdbc.queryForObject(contains("select exists"), eq(Boolean.class), eq(ORDER), eq(batch.reason())))
                .thenReturn(exists);
    }
}
