package io.pickuppact.fulfillment.domain;

public enum FulfillmentState {
    RECEIVED,
    ACCEPTED,
    PREPARING,
    READY,
    PICKED_UP,
    CANCELLED,
    CANCELLATION_REVIEW
}
