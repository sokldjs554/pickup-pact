package io.pickuppact.fulfillment.application;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.mockito.Mockito.*;

import com.fasterxml.jackson.databind.ObjectMapper;
import io.pickuppact.fulfillment.infra.MerchantFulfillmentRepository;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.NoSuchElementException;
import java.util.Optional;
import java.util.UUID;
import org.junit.jupiter.api.Test;

class MerchantFulfillmentServiceTest {
    private final MerchantFulfillmentRepository repository = mock(MerchantFulfillmentRepository.class);
    private final Clock clock = Clock.fixed(Instant.parse("2026-09-23T03:00:00Z"), ZoneOffset.UTC);
    private final MerchantFulfillmentService service = new MerchantFulfillmentService(repository, clock);
    private final ObjectMapper objectMapper = new ObjectMapper();

    @Test
    void cancellationBeforeMerchantConfirmationIsAValidNoop() throws Exception {
        UUID orderId = UUID.randomUUID();
        when(repository.recordInbox(anyString(), eq(orderId), eq("CommitmentCancelled"), any()))
                .thenReturn(true);
        when(repository.find(orderId)).thenReturn(Optional.empty());

        var result = service.handleCommitmentEvent(
                "cancel-before-confirm",
                orderId,
                "CommitmentCancelled",
                Instant.parse("2026-09-23T02:59:00Z"),
                objectMapper.readTree("{\"reason\":\"customer_request\"}")
        );

        assertEquals(MerchantFulfillmentService.IntakeResult.APPLIED, result);
        verify(repository).recordInbox(
                eq("cancel-before-confirm"),
                eq(orderId),
                eq("CommitmentCancelled"),
                any()
        );
        verify(repository).find(orderId);
        verifyNoMoreInteractions(repository);
    }

    @Test
    void duplicatePreConfirmCancellationIsCollapsedByInbox() throws Exception {
        UUID orderId = UUID.randomUUID();
        when(repository.recordInbox(anyString(), eq(orderId), eq("CommitmentCancelled"), any()))
                .thenReturn(false);

        var result = service.handleCommitmentEvent(
                "cancel-before-confirm",
                orderId,
                "CommitmentCancelled",
                Instant.parse("2026-09-23T02:59:00Z"),
                objectMapper.readTree("{\"reason\":\"customer_request\"}")
        );

        assertEquals(MerchantFulfillmentService.IntakeResult.DUPLICATE_NOOP, result);
        verify(repository, never()).find(orderId);
    }

    @Test
    void rescheduleWithoutConfirmedMerchantOrderRemainsACausalContractError() throws Exception {
        UUID orderId = UUID.randomUUID();
        when(repository.recordInbox(anyString(), eq(orderId), eq("PickupRescheduled"), any()))
                .thenReturn(true);
        when(repository.find(orderId)).thenReturn(Optional.empty());

        assertThrows(NoSuchElementException.class, () -> service.handleCommitmentEvent(
                "reschedule-without-confirm",
                orderId,
                "PickupRescheduled",
                Instant.parse("2026-09-23T03:01:00Z"),
                objectMapper.readTree("{\"pickup_at\":\"2026-09-23T03:30:00Z\"}")
        ));
    }
}
