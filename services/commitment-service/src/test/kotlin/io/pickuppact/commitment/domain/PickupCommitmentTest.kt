package io.pickuppact.commitment.domain

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import java.time.Instant
import java.util.UUID

class PickupCommitmentTest {
    @Test
    fun heldCommitmentMustReceivePaymentAuthorizationBeforeConfirmation() {
        val held = PickupCommitment(
            UUID.randomUUID(),
            "store-1",
            Instant.now().plusSeconds(600),
            2,
            "lease-1",
            false,
            CommitmentState.HELD
        )

        assertThrows(IllegalStateException::class.java) { held.confirm(Instant.now()) }

        val paid = held.authorizePayment()
        assertTrue(paid.paymentAuthorized)
        assertEquals(1, paid.version)

        val confirmed = paid.confirm(Instant.now())
        assertEquals(CommitmentState.CONFIRMED, confirmed.state)
        assertEquals(2, confirmed.version)
        assertEquals(PickupPactStatus.ACTIVE, confirmed.pact!!.status)
    }

    @Test
    fun paymentAuthorizationCannotBeAttachedTwice() {
        val held = PickupCommitment(
            UUID.randomUUID(),
            "store-1",
            Instant.now().plusSeconds(600),
            1,
            "lease-2",
            false,
            CommitmentState.HELD
        )
        val paid = held.authorizePayment()
        assertThrows(IllegalStateException::class.java) { paid.authorizePayment() }
    }

    @Test
    fun confirmedCommitmentCanBeClaimedOnlyOnceAndThenCannotBeCancelled() {
        val confirmed = PickupCommitment(
            UUID.randomUUID(),
            "store-1",
            Instant.now().plusSeconds(600),
            1,
            "lease-claim",
            true,
            CommitmentState.CONFIRMED
        )

        val pickedUp = confirmed.claimPickup()

        assertEquals(CommitmentState.PICKED_UP, pickedUp.state)
        assertEquals(1, pickedUp.version)
        assertThrows(IllegalStateException::class.java) { pickedUp.claimPickup() }
        assertThrows(IllegalStateException::class.java) { pickedUp.cancelHeld() }
    }

    @Test
    fun confirmedCancellationNeedsMatchingMerchantDecision() {
        val confirmed = PickupCommitment(
            UUID.randomUUID(),
            "store-1",
            Instant.now().plusSeconds(600),
            1,
            "lease-cancel-authority",
            true,
            CommitmentState.CONFIRMED,
            pact = PickupPactPolicy.issue(Instant.now().plusSeconds(600))
        )
        val requestId = UUID.randomUUID()
        val requested = confirmed.requestCancellation(requestId, Instant.now())

        assertEquals(CommitmentState.CONFIRMED, requested.state)
        assertEquals(requestId, requested.cancellationRequestId)
        assertThrows(IllegalStateException::class.java) {
            requested.approveCancellation(UUID.randomUUID())
        }
        assertThrows(IllegalStateException::class.java) { requested.claimPickup() }

        val rejected = requested.rejectCancellation(requestId)
        assertEquals(CommitmentState.CONFIRMED, rejected.state)
        assertEquals(null, rejected.cancellationRequestId)

        val secondRequest = rejected.requestCancellation(requestId, Instant.now())
        val cancelled = secondRequest.approveCancellation(requestId)
        assertEquals(CommitmentState.CANCELLED, cancelled.state)
        assertEquals(PickupPactStatus.CANCELLED, cancelled.pact!!.status)
        assertEquals(null, cancelled.cancellationRequestId)
    }

    @Test
    fun confirmedCommitmentCanBecomeAtRiskWithoutLosingHistory() {
        val confirmed = PickupCommitment(
            UUID.randomUUID(),
            "store-1",
            Instant.now().plusSeconds(600),
            2,
            "lease-3",
            true,
            CommitmentState.CONFIRMED
        )
        val atRisk = confirmed.markAtRisk()
        assertEquals(CommitmentState.AT_RISK, atRisk.state)
        assertEquals(1, atRisk.version)
        assertTrue(atRisk.paymentAuthorized)
        assertFalse(confirmed.state == CommitmentState.AT_RISK)
    }
}
