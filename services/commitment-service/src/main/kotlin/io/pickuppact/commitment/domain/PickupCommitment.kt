package io.pickuppact.commitment.domain

import java.time.Instant
import java.util.UUID

enum class CommitmentState { HELD, CONFIRMED, PICKED_UP, CANCELLED, AT_RISK }

data class PickupCommitment(
    val id: UUID,
    val storeId: String,
    val pickupAt: Instant,
    val units: Int,
    val leaseToken: String,
    val paymentAuthorized: Boolean,
    val state: CommitmentState,
    val version: Long = 0
) {
    init {
        require(units > 0) { "units must be positive" }
    }

    fun authorizePayment(): PickupCommitment {
        require(state == CommitmentState.HELD) { "payment can only be attached to a HELD commitment" }
        require(!paymentAuthorized) { "payment is already authorized" }
        return copy(paymentAuthorized = true, version = version + 1)
    }

    fun confirm(now: Instant): PickupCommitment {
        require(state == CommitmentState.HELD) { "only HELD can be confirmed" }
        require(paymentAuthorized) { "payment authorization is required" }
        require(now.isBefore(pickupAt)) { "cannot confirm after pickup time" }
        return copy(state = CommitmentState.CONFIRMED, version = version + 1)
    }

    fun claimPickup(): PickupCommitment {
        require(state == CommitmentState.CONFIRMED) { "pickup can only be claimed from CONFIRMED" }
        return copy(state = CommitmentState.PICKED_UP, version = version + 1)
    }

    fun cancel(): PickupCommitment {
        require(state != CommitmentState.CANCELLED) { "already cancelled" }
        require(state != CommitmentState.PICKED_UP) { "picked up commitment cannot be cancelled" }
        return copy(state = CommitmentState.CANCELLED, version = version + 1)
    }

    fun markAtRisk(): PickupCommitment =
        if (state == CommitmentState.CONFIRMED) copy(state = CommitmentState.AT_RISK, version = version + 1) else this
}
