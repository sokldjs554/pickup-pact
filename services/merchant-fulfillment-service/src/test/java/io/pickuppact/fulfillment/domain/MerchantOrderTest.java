package io.pickuppact.fulfillment.domain;

import static org.junit.jupiter.api.Assertions.*;

import java.time.Instant;
import java.util.UUID;
import org.junit.jupiter.api.Test;

class MerchantOrderTest {
    private MerchantOrder order() {
        return MerchantOrder.receive(
                UUID.randomUUID(),
                "store-1",
                Instant.parse("2026-09-23T03:30:00Z"),
                2
        );
    }

    @Test
    void preparationCannotStartBeforeJitWindow() {
        var accepted = order().accept(Instant.parse("2026-09-23T03:20:00Z"));

        assertThrows(
                IllegalStateException.class,
                () -> accepted.start(Instant.parse("2026-09-23T03:25:59Z"))
        );
    }

    @Test
    void lateStartIsAllowedButFlagged() {
        var accepted = order().accept(Instant.parse("2026-09-23T03:20:00Z"));
        var transition = accepted.start(Instant.parse("2026-09-23T03:29:30Z"));

        assertEquals(FulfillmentState.PREPARING, transition.order().state());
        assertEquals(FulfillmentAnomalyCode.STARTED_LATE, transition.anomaly());
    }

    @Test
    void readyTooEarlyAndReadyLateAreBothObservable() {
        var accepted = order().accept(Instant.parse("2026-09-23T03:20:00Z"));

        var earlyPreparing = accepted.start(Instant.parse("2026-09-23T03:26:00Z")).order();
        var early = earlyPreparing.ready(Instant.parse("2026-09-23T03:26:30Z"));
        assertEquals(JitPreparationPolicy.ReadyQuality.EARLY, early.quality());
        assertEquals(FulfillmentAnomalyCode.READY_TOO_EARLY, early.anomaly());

        var latePreparing = accepted.start(Instant.parse("2026-09-23T03:29:30Z")).order();
        var late = latePreparing.ready(Instant.parse("2026-09-23T03:33:01Z"));
        assertEquals(JitPreparationPolicy.ReadyQuality.LATE, late.quality());
        assertEquals(FulfillmentAnomalyCode.READY_LATE, late.anomaly());
    }

    @Test
    void cancellationBeforePreparationIsAutomatic() {
        var accepted = order().accept(Instant.parse("2026-09-23T03:20:00Z"));
        var cancelled = accepted.requestCancellation(Instant.parse("2026-09-23T03:24:00Z"));

        assertEquals(FulfillmentState.CANCELLED, cancelled.order().state());
        assertNull(cancelled.anomaly());
    }

    @Test
    void cancellationAfterPreparationRequiresReview() {
        var preparing = order()
                .accept(Instant.parse("2026-09-23T03:20:00Z"))
                .start(Instant.parse("2026-09-23T03:26:00Z"))
                .order();

        var cancellation = preparing.requestCancellation(Instant.parse("2026-09-23T03:27:00Z"));

        assertEquals(FulfillmentState.CANCELLATION_REVIEW, cancellation.order().state());
        assertEquals(FulfillmentAnomalyCode.CANCEL_AFTER_PREPARATION, cancellation.anomaly());
    }

    @Test
    void rescheduleIsSafeBeforePreparationAndRejectedAfterStart() {
        var accepted = order().accept(Instant.parse("2026-09-23T03:20:00Z"));
        var rescheduled = accepted.reschedule(Instant.parse("2026-09-23T03:35:00Z"));

        assertEquals(Instant.parse("2026-09-23T03:35:00Z"), rescheduled.pickupAt());
        assertEquals(Instant.parse("2026-09-23T03:34:00Z"), rescheduled.window().targetReadyAt());

        var preparing = rescheduled.start(Instant.parse("2026-09-23T03:31:00Z")).order();
        assertThrows(
                IllegalStateException.class,
                () -> preparing.reschedule(Instant.parse("2026-09-23T03:40:00Z"))
        );
    }
}
