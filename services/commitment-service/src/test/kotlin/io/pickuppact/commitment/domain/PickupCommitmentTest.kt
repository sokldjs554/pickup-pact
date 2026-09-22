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

        assertThrows(IllegalArgumentException::class.java) { held.confirm(Instant.now()) }

        val paid = held.authorizePayment()
        assertTrue(paid.paymentAuthorized)
        assertEquals(1, paid.version)

        val confirmed = paid.confirm(Instant.now())
        assertEquals(CommitmentState.CONFIRMED, confirmed.state)
        assertEquals(2, confirmed.version)
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
        assertThrows(IllegalArgumentException::class.java) { paid.authorizePayment() }
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
        assertThrows(IllegalArgumentException::class.java) { pickedUp.claimPickup() }
        assertThrows(IllegalArgumentException::class.java) { pickedUp.cancel() }
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
