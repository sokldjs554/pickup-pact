package io.pickuppact.fulfillment.domain;

import static org.junit.jupiter.api.Assertions.assertEquals;

import java.time.Instant;
import org.junit.jupiter.api.Test;

class JitPreparationPolicyTest {
    @Test
    void derivesPreparationWindowFromPickupTimeAndWorkload() {
        var pickupAt = Instant.parse("2026-09-23T03:30:00Z");
        var window = JitPreparationPolicy.forOrder(pickupAt, 2);

        assertEquals(90, window.preparationSeconds());
        assertEquals(Instant.parse("2026-09-23T03:26:00Z"), window.earliestStartAt());
        assertEquals(Instant.parse("2026-09-23T03:29:00Z"), window.targetReadyAt());
        assertEquals(Instant.parse("2026-09-23T03:33:00Z"), window.latestReadyAt());
    }

    @Test
    void classifiesEarlyOnTimeAndLateReadiness() {
        var pickupAt = Instant.parse("2026-09-23T03:30:00Z");
        var window = JitPreparationPolicy.forOrder(pickupAt, 2);

        assertEquals(
                JitPreparationPolicy.ReadyQuality.EARLY,
                JitPreparationPolicy.classifyReady(window, Instant.parse("2026-09-23T03:26:30Z"))
        );
        assertEquals(
                JitPreparationPolicy.ReadyQuality.ON_TIME,
                JitPreparationPolicy.classifyReady(window, Instant.parse("2026-09-23T03:29:30Z"))
        );
        assertEquals(
                JitPreparationPolicy.ReadyQuality.LATE,
                JitPreparationPolicy.classifyReady(window, Instant.parse("2026-09-23T03:33:01Z"))
        );
    }
}
