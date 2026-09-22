package io.pickuppact.commitment.domain

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import java.time.Duration
import java.time.Instant

class PickupPactTest {
    @Test
    fun issueCreatesExplicitThreeMinuteGuaranteeAndCompensation() {
        val pickupAt = Instant.parse("2026-09-22T03:00:00Z")

        val pact = PickupPactPolicy.issue(pickupAt)

        assertEquals(pickupAt, pact.promisedAt)
        assertEquals(Duration.ofMinutes(3), Duration.between(pact.promisedAt, pact.latestAt))
        assertEquals(500, pact.compensationPoints)
        assertEquals(1, pact.version)
        assertEquals(PickupPactStatus.ACTIVE, pact.status)
        assertFalse(pact.compensationGranted)
    }

    @Test
    fun renegotiationVersionsThePromiseInsteadOfSilentlyOverwritingIt() {
        val pact = PickupPactPolicy.issue(Instant.parse("2026-09-22T03:00:00Z"))
        val newPickupAt = Instant.parse("2026-09-22T03:05:00Z")

        val renegotiated = pact.renegotiate(newPickupAt)

        assertEquals(2, renegotiated.version)
        assertEquals(newPickupAt, renegotiated.promisedAt)
        assertEquals(newPickupAt.plusSeconds(180), renegotiated.latestAt)
        assertEquals(PickupPactStatus.ACTIVE, renegotiated.status)
    }

    @Test
    fun breachBeforeGuaranteeDeadlineIsRejected() {
        val now = Instant.now()
        val pact = PickupPactPolicy.issue(now.plusSeconds(600))

        assertThrows(IllegalStateException::class.java) {
            pact.breach(now)
        }
        assertEquals(PickupPactStatus.ACTIVE, pact.status)
        assertFalse(pact.compensationGranted)
    }

    @Test
    fun breachAfterDeadlineGrantsCompensationOnlyOnce() {
        val now = Instant.now()
        val pact = PickupPactPolicy.issue(now.minusSeconds(600))

        val breached = pact.breach(now)

        assertEquals(PickupPactStatus.COMPENSATED, breached.status)
        assertTrue(breached.compensationGranted)
        assertThrows(IllegalStateException::class.java) { breached.breach(now.plusSeconds(1)) }
        assertEquals(PickupPactStatus.COMPENSATED, breached.fulfill().status)
    }

    @Test
    fun cancellationIsExplicitAndCannotCancelFulfilledPact() {
        val cancelled = PickupPactPolicy.issue(Instant.now().plusSeconds(600)).cancel()
        assertEquals(PickupPactStatus.CANCELLED, cancelled.status)

        val fulfilled = PickupPactPolicy.issue(Instant.now().plusSeconds(600)).fulfill()
        assertThrows(IllegalStateException::class.java) { fulfilled.cancel() }
    }
}
