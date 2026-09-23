package io.pickuppact.fulfillment.application;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.mockito.Mockito.*;

import com.fasterxml.jackson.databind.ObjectMapper;
import io.pickuppact.fulfillment.domain.FulfillmentState;
import io.pickuppact.fulfillment.domain.MerchantOrder;
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
    void cancellationRequestBeforePreparationEmitsMerchantApproval() throws Exception {
        UUID orderId = UUID.randomUUID();
        UUID requestId = UUID.randomUUID();
        MerchantOrder received = MerchantOrder.receive(
                orderId,
                "store-1",
                Instant.parse("2026-09-23T03:30:00Z"),
                2
        );
        when(repository.recordInbox(anyString(), eq(orderId), eq("CancellationRequested"), any()))
                .thenReturn(true);
        when(repository.find(orderId)).thenReturn(Optional.of(received));

        var result = service.handleCommitmentEvent(
                "cancel-request-1",
                orderId,
                "CancellationRequested",
                Instant.parse("2026-09-23T03:01:00Z"),
                objectMapper.readTree("{\"request_id\":\"" + requestId + "\"}")
        );

        assertEquals(MerchantFulfillmentService.IntakeResult.APPLIED, result);
        verify(repository).update(argThat(order -> order.state() == FulfillmentState.CANCELLED));
        verify(repository).appendOutbox(
                eq(orderId),
                eq("MerchantCancellationApproved"),
                argThat(payload -> payload instanceof java.util.Map<?, ?>
                        && requestId.toString().equals(((java.util.Map<?, ?>) payload).get("request_id"))),
                any()
        );
        verify(repository, never()).recordAnomaly(any(), any(), anyString(), any());
    }

    @Test
    void cancellationRequestAfterPreparationEmitsRejectionWithoutCancellingMerchantOrder() throws Exception {
        UUID orderId = UUID.randomUUID();
        UUID requestId = UUID.randomUUID();
        MerchantOrder preparing = MerchantOrder.receive(
                orderId,
                "store-1",
                Instant.parse("2026-09-23T03:30:00Z"),
                2
        ).accept(Instant.parse("2026-09-23T03:20:00Z"))
         .start(Instant.parse("2026-09-23T03:26:00Z"))
         .order();
        when(repository.recordInbox(anyString(), eq(orderId), eq("CancellationRequested"), any()))
                .thenReturn(true);
        when(repository.find(orderId)).thenReturn(Optional.of(preparing));

        var result = service.handleCommitmentEvent(
                "cancel-request-2",
                orderId,
                "CancellationRequested",
                Instant.parse("2026-09-23T03:27:00Z"),
                objectMapper.readTree("{\"request_id\":\"" + requestId + "\"}")
        );

        assertEquals(MerchantFulfillmentService.IntakeResult.APPLIED, result);
        verify(repository, never()).update(any());
        verify(repository).appendOutbox(
                eq(orderId),
                eq("MerchantCancellationRejected"),
                argThat(payload -> payload instanceof java.util.Map<?, ?>
                        && requestId.toString().equals(((java.util.Map<?, ?>) payload).get("request_id"))
                        && "PREPARATION_ALREADY_STARTED".equals(((java.util.Map<?, ?>) payload).get("reason"))),
                any()
        );
        verify(repository).recordAnomaly(
                eq(orderId),
                any(),
                contains("cancellation rejected"),
                any()
        );
    }

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
