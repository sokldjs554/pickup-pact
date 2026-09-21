package io.pickuppact.commitment.domain

import org.junit.jupiter.api.Assertions.*
import org.junit.jupiter.api.Test
import java.time.Instant
import java.util.UUID

class PickupCommitmentTest {
    @Test
    fun confirmedCommitmentRequiresPaymentAuthorization() {
        val held = PickupCommitment(UUID.randomUUID(), "store-1", Instant.now().plusSeconds(600), 2, "lease-1", false, CommitmentState.HELD)
        assertThrows(IllegalArgumentException::class.java) { held.confirm(Instant.now()) }
    }

    @Test
    fun confirmedCommitmentCanBecomeAtRiskWithoutLosingHistory() {
        val confirmed = PickupCommitment(UUID.randomUUID(), "store-1", Instant.now().plusSeconds(600), 2, "lease-1", true, CommitmentState.CONFIRMED)
        assertEquals(CommitmentState.AT_RISK, confirmed.markAtRisk().state)
        assertEquals(1, confirmed.markAtRisk().version)
    }
}
