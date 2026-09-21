package io.pickuppact.ledger.application;

import io.pickuppact.ledger.domain.LedgerPostingPolicy;
import io.pickuppact.ledger.infra.LedgerRepository;
import java.math.BigDecimal;
import java.util.Optional;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

class LedgerPostingServiceTest {
    @Test
    void exactRedeliveryIsNoopAfterAtomicInsertConflict() {
        var repository = mock(LedgerRepository.class);
        var service = new LedgerPostingService(repository);
        var batch = LedgerPostingPolicy.settlement("evt-1", "order-1", new BigDecimal("12000.00"));

        when(repository.appendIfAbsent(batch)).thenReturn(false);
        when(repository.fingerprint("evt-1")).thenReturn(Optional.of(batch.semanticFingerprint()));

        assertEquals(LedgerPostingService.Result.DUPLICATE_NOOP, service.post(batch));
    }

    @Test
    void reusedEventIdWithDifferentMeaningIsConflict() {
        var repository = mock(LedgerRepository.class);
        var service = new LedgerPostingService(repository);
        var batch = LedgerPostingPolicy.settlement("evt-2", "order-1", new BigDecimal("12000"));

        when(repository.appendIfAbsent(batch)).thenReturn(false);
        when(repository.fingerprint("evt-2")).thenReturn(Optional.of("different-fingerprint"));

        assertEquals(LedgerPostingService.Result.CONFLICTING_EVENT_ID, service.post(batch));
    }
}
