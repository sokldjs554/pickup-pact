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
    val version: Long = 0,
    val totalAmount: Int = 0,
    val idempotencyKey: String = "",
    val requestFingerprint: String = "",
    val pact: PickupPact? = null,
    val cancellationRequestId: UUID? = null,
    val cancellationRequestedAt: Instant? = null
) {
    init {
        require(units > 0) { "units must be positive" }
        require(totalAmount >= 0) { "totalAmount must not be negative" }
    }

    fun authorizePayment(): PickupCommitment {
        check(state == CommitmentState.HELD) { "payment can only be attached to a HELD commitment" }
        check(!paymentAuthorized) { "payment is already authorized" }
        return copy(paymentAuthorized = true, version = version + 1)
    }

    fun confirm(now: Instant): PickupCommitment {
        check(state == CommitmentState.HELD) { "only HELD can be confirmed" }
        check(paymentAuthorized) { "payment authorization is required" }
        check(now.isBefore(pickupAt)) { "cannot confirm after pickup time" }
        return copy(
            state = CommitmentState.CONFIRMED,
            pact = PickupPactPolicy.issue(pickupAt),
            version = version + 1
        )
    }

    fun renegotiate(newPickupAt: Instant, newLeaseToken: String): PickupCommitment {
        check(cancellationRequestId == null) { "pickup cannot be rescheduled while cancellation is pending" }
        check(state in setOf(CommitmentState.CONFIRMED, CommitmentState.AT_RISK)) {
            "only confirmed or at-risk commitments can be renegotiated"
        }
        val currentPact = checkNotNull(pact) { "pickup pact is required before renegotiation" }
        return copy(
            pickupAt = newPickupAt,
            leaseToken = newLeaseToken,
            state = CommitmentState.CONFIRMED,
            pact = currentPact.renegotiate(newPickupAt),
            version = version + 1
        )
    }

    fun breachPact(observedAt: Instant): PickupCommitment {
        check(state in setOf(CommitmentState.CONFIRMED, CommitmentState.AT_RISK)) {
            "only active pickup commitments can breach a pact"
        }
        val currentPact = checkNotNull(pact) { "pickup pact has not been issued" }
        return copy(pact = currentPact.breach(observedAt), version = version + 1)
    }

    fun claimPickup(): PickupCommitment {
        check(cancellationRequestId == null) { "pickup cannot be claimed while cancellation is pending" }
        check(state == CommitmentState.CONFIRMED) { "pickup can only be claimed from CONFIRMED" }
        return copy(
            state = CommitmentState.PICKED_UP,
            pact = pact?.fulfill(),
            version = version + 1
        )
    }

    fun cancelHeld(): PickupCommitment {
        check(state == CommitmentState.HELD) { "only HELD commitments can cancel without merchant authority" }
        return copy(
            state = CommitmentState.CANCELLED,
            pact = pact?.cancel(),
            cancellationRequestId = null,
            cancellationRequestedAt = null,
            version = version + 1
        )
    }

    fun requestCancellation(requestId: UUID, requestedAt: Instant): PickupCommitment {
        check(state in setOf(CommitmentState.CONFIRMED, CommitmentState.AT_RISK)) {
            "merchant authority is required only for active confirmed commitments"
        }
        if (cancellationRequestId != null) {
            check(cancellationRequestId == requestId) { "another cancellation request is already pending" }
            return this
        }
        return copy(
            cancellationRequestId = requestId,
            cancellationRequestedAt = requestedAt,
            version = version + 1
        )
    }

    fun approveCancellation(requestId: UUID): PickupCommitment {
        check(cancellationRequestId == requestId) { "stale or unknown cancellation approval" }
        check(state in setOf(CommitmentState.CONFIRMED, CommitmentState.AT_RISK)) {
            "only active confirmed commitments can be cancelled"
        }
        return copy(
            state = CommitmentState.CANCELLED,
            pact = pact?.cancel(),
            cancellationRequestId = null,
            cancellationRequestedAt = null,
            version = version + 1
        )
    }

    fun rejectCancellation(requestId: UUID): PickupCommitment {
        check(cancellationRequestId == requestId) { "stale or unknown cancellation rejection" }
        check(state in setOf(CommitmentState.CONFIRMED, CommitmentState.AT_RISK)) {
            "only active confirmed commitments can reject cancellation"
        }
        return copy(
            cancellationRequestId = null,
            cancellationRequestedAt = null,
            version = version + 1
        )
    }

    fun markAtRisk(): PickupCommitment =
        if (state == CommitmentState.CONFIRMED) copy(state = CommitmentState.AT_RISK, version = version + 1) else this
}
