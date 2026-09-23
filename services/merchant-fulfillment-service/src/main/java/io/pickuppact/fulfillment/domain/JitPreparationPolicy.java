package io.pickuppact.fulfillment.domain;

import java.time.Instant;

public final class JitPreparationPolicy {
    private JitPreparationPolicy() {}

    public static final long TARGET_READY_LEAD_SECONDS = 60;
    public static final long EARLY_READY_TOLERANCE_SECONDS = 120;
    public static final long START_SLACK_SECONDS = 90;
    public static final long GUARANTEE_GRACE_SECONDS = 180;

    public record Window(
            int preparationSeconds,
            Instant earliestStartAt,
            Instant targetReadyAt,
            Instant latestReadyAt
    ) {}

    public enum ReadyQuality { EARLY, ON_TIME, LATE }

    public static Window forOrder(Instant pickupAt, int capacityUnits) {
        if (capacityUnits <= 0) throw new IllegalArgumentException("capacityUnits must be positive");
        int preparationSeconds = Math.max(60, Math.min(600, capacityUnits * 45));
        Instant targetReadyAt = pickupAt.minusSeconds(TARGET_READY_LEAD_SECONDS);
        Instant earliestStartAt = targetReadyAt.minusSeconds(preparationSeconds + START_SLACK_SECONDS);
        Instant latestReadyAt = pickupAt.plusSeconds(GUARANTEE_GRACE_SECONDS);
        return new Window(preparationSeconds, earliestStartAt, targetReadyAt, latestReadyAt);
    }

    public static ReadyQuality classifyReady(Window window, Instant readyAt) {
        Instant tooEarlyBefore = window.targetReadyAt().minusSeconds(EARLY_READY_TOLERANCE_SECONDS);
        if (readyAt.isBefore(tooEarlyBefore)) return ReadyQuality.EARLY;
        if (readyAt.isAfter(window.latestReadyAt())) return ReadyQuality.LATE;
        return ReadyQuality.ON_TIME;
    }
}
