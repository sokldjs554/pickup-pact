package io.pickuppact.ledger.application;

import io.pickuppact.ledger.domain.LedgerPostingPolicy;
import io.pickuppact.ledger.infra.LedgerRepository;
import java.math.BigDecimal;
import java.util.Optional;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.Mockito.*;

class LedgerPostingServiceTest {
    @Test
    void exactRedeliveryIsNoopBeforeRevalidatingSpentSourceBalance() {
        var repository = mock(LedgerRepository.class);
        var service = new LedgerPostingService(repository);
        var batch = LedgerPostingPolicy.reverseSettlement(
                "evt-1", "order-1", new BigDecimal("3000"), "settle-1"
        );

        when(repository.fingerprint("evt-1")).thenReturn(Optional.of(batch.semanticFingerprint()));

        assertEquals(LedgerPostingService.Result.DUPLICATE_NOOP, service.post(batch));
        verify(repository, never()).reversalAllowed(any());
        verify(repository, never()).appendIfAbsent(any());
    }

    @Test
    void reusedEventIdWithDifferentMeaningIsQuarantined() {
        var repository = mock(LedgerRepository.class);
        var service = new LedgerPostingService(repository);
        var batch = LedgerPostingPolicy.settlement("evt-2", "order-1", new BigDecimal("12000"));

        when(repository.fingerprint("evt-2")).thenReturn(Optional.of("different-fingerprint"));

        assertEquals(LedgerPostingService.Result.CONFLICTING_EVENT_ID, service.post(batch));
        verify(repository).recordConflict(batch, "different-fingerprint");
        verify(repository, never()).appendIfAbsent(any());
    }

    @Test
    void reversalBeyondSourceBalanceIsRejectedBeforeAppend() {
        var repository = mock(LedgerRepository.class);
        var service = new LedgerPostingService(repository);
        var batch = LedgerPostingPolicy.reverseSettlement(
                "reverse-too-much", "order-1", new BigDecimal("9001"), "settle-1"
        );

        when(repository.fingerprint(batch.eventId())).thenReturn(Optional.empty());
        when(repository.reversalAllowed(batch)).thenReturn(false);

        assertEquals(LedgerPostingService.Result.SOURCE_POSTING_CONFLICT, service.post(batch));
        verify(repository, never()).appendIfAbsent(any());
    }
}
