package io.pickuppact.fulfillment.domain;

import java.time.Instant;
import java.util.Objects;
import java.util.UUID;

public record MerchantOrder(
        UUID orderId,
        String storeId,
        Instant pickupAt,
        int capacityUnits,
        FulfillmentState state,
        JitPreparationPolicy.Window window,
        Instant acceptedAt,
        Instant startedAt,
        Instant readyAt,
        Instant pickedUpAt,
        Instant cancellationRequestedAt,
        long version
) {
    public MerchantOrder {
        Objects.requireNonNull(orderId);
        if (storeId == null || storeId.isBlank()) throw new IllegalArgumentException("storeId must not be blank");
        Objects.requireNonNull(pickupAt);
        if (capacityUnits <= 0) throw new IllegalArgumentException("capacityUnits must be positive");
        Objects.requireNonNull(state);
        Objects.requireNonNull(window);
    }

    public static MerchantOrder receive(UUID orderId, String storeId, Instant pickupAt, int capacityUnits) {
        return new MerchantOrder(
                orderId,
                storeId,
                pickupAt,
                capacityUnits,
                FulfillmentState.RECEIVED,
                JitPreparationPolicy.forOrder(pickupAt, capacityUnits),
                null, null, null, null, null, 0
        );
    }

    public MerchantOrder reschedule(Instant newPickupAt) {
        if (state != FulfillmentState.RECEIVED && state != FulfillmentState.ACCEPTED) {
            throw new IllegalStateException("pickup time cannot change after preparation has started: current=" + state);
        }
        return new MerchantOrder(
                orderId, storeId, newPickupAt, capacityUnits, state,
                JitPreparationPolicy.forOrder(newPickupAt, capacityUnits),
                acceptedAt, startedAt, readyAt, pickedUpAt, cancellationRequestedAt, version + 1
        );
    }

    public MerchantOrder accept(Instant now) {
        if (state == FulfillmentState.ACCEPTED) return this;
        requireState(FulfillmentState.RECEIVED, "only RECEIVED orders can be accepted");
        return copy(FulfillmentState.ACCEPTED, now, startedAt, readyAt, pickedUpAt, cancellationRequestedAt);
    }

    public StartTransition start(Instant now) {
        requireState(FulfillmentState.ACCEPTED, "only ACCEPTED orders can start preparation");
        if (now.isBefore(window.earliestStartAt())) {
            throw new IllegalStateException("preparation window has not opened");
        }
        MerchantOrder next = copy(FulfillmentState.PREPARING, acceptedAt, now, readyAt, pickedUpAt, cancellationRequestedAt);
        FulfillmentAnomalyCode anomaly = now.isAfter(window.targetReadyAt())
                ? FulfillmentAnomalyCode.STARTED_LATE
                : null;
        return new StartTransition(next, anomaly);
    }

    public ReadyTransition ready(Instant now) {
        requireState(FulfillmentState.PREPARING, "only PREPARING orders can become READY");
        MerchantOrder next = copy(FulfillmentState.READY, acceptedAt, startedAt, now, pickedUpAt, cancellationRequestedAt);
        var quality = JitPreparationPolicy.classifyReady(window, now);
        FulfillmentAnomalyCode anomaly = switch (quality) {
            case EARLY -> FulfillmentAnomalyCode.READY_TOO_EARLY;
            case LATE -> FulfillmentAnomalyCode.READY_LATE;
            case ON_TIME -> null;
        };
        return new ReadyTransition(next, quality, anomaly);
    }

    public CancellationDecision decideCancellation(Instant now) {
        return switch (state) {
            case RECEIVED, ACCEPTED -> new CancellationDecision(
                    copy(FulfillmentState.CANCELLED, acceptedAt, startedAt, readyAt, pickedUpAt, now),
                    true,
                    "APPROVED_BEFORE_PREPARATION",
                    null
            );
            case PREPARING, READY, PICKED_UP, CANCELLATION_REVIEW -> new CancellationDecision(
                    this,
                    false,
                    "PREPARATION_ALREADY_STARTED",
                    FulfillmentAnomalyCode.CANCEL_AFTER_PREPARATION
            );
            case CANCELLED -> new CancellationDecision(
                    this,
                    true,
                    "ALREADY_CANCELLED",
                    null
            );
        };
    }

    public CancelTransition requestCancellation(Instant now) {
        if (state == FulfillmentState.CANCELLED || state == FulfillmentState.CANCELLATION_REVIEW) {
            return new CancelTransition(this, state == FulfillmentState.CANCELLATION_REVIEW
                    ? FulfillmentAnomalyCode.CANCEL_AFTER_PREPARATION : null);
        }
        return switch (state) {
            case RECEIVED, ACCEPTED -> new CancelTransition(
                    copy(FulfillmentState.CANCELLED, acceptedAt, startedAt, readyAt, pickedUpAt, now),
                    null
            );
            case PREPARING, READY -> new CancelTransition(
                    copy(FulfillmentState.CANCELLATION_REVIEW, acceptedAt, startedAt, readyAt, pickedUpAt, now),
                    FulfillmentAnomalyCode.CANCEL_AFTER_PREPARATION
            );
            case PICKED_UP -> throw new IllegalStateException("picked-up order cannot be cancelled");
            default -> throw new IllegalStateException("unsupported cancellation state: " + state);
        };
    }

    public MerchantOrder pickup(Instant now) {
        requireState(FulfillmentState.READY, "only READY orders can be picked up");
        return copy(FulfillmentState.PICKED_UP, acceptedAt, startedAt, readyAt, now, cancellationRequestedAt);
    }

    private MerchantOrder copy(
            FulfillmentState nextState,
            Instant nextAcceptedAt,
            Instant nextStartedAt,
            Instant nextReadyAt,
            Instant nextPickedUpAt,
            Instant nextCancellationRequestedAt
    ) {
        return new MerchantOrder(
                orderId, storeId, pickupAt, capacityUnits, nextState, window,
                nextAcceptedAt, nextStartedAt, nextReadyAt, nextPickedUpAt,
                nextCancellationRequestedAt, version + 1
        );
    }

    private void requireState(FulfillmentState required, String message) {
        if (state != required) throw new IllegalStateException(message + ": current=" + state);
    }

    public record StartTransition(MerchantOrder order, FulfillmentAnomalyCode anomaly) {}
    public record ReadyTransition(
            MerchantOrder order,
            JitPreparationPolicy.ReadyQuality quality,
            FulfillmentAnomalyCode anomaly
    ) {}
    public record CancelTransition(MerchantOrder order, FulfillmentAnomalyCode anomaly) {}
    public record CancellationDecision(
            MerchantOrder order,
            boolean approved,
            String reason,
            FulfillmentAnomalyCode anomaly
    ) {}
}
