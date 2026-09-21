package io.pickuppact.commitment.application

import io.pickuppact.commitment.domain.PickupCommitment
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import reactor.core.publisher.Mono
import reactor.test.StepVerifier
import java.time.Instant
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap

class CommitmentServiceTest {
    private class FakeCapacity : CapacityLeasePort {
        val acquired = mutableListOf<Long>()
        val released = mutableListOf<String>()
        var nextToken = "lease-test"

        override fun acquire(
            storeId: String,
            pickupAt: Instant,
            units: Int,
            ttlSeconds: Long
        ): Mono<String> {
            acquired += ttlSeconds
            return Mono.just(nextToken)
        }

        override fun release(token: String): Mono<Void> {
            released += token
            return Mono.empty()
        }
    }

    private class FakeRepository : CommitmentRepository {
        val rows = ConcurrentHashMap<UUID, PickupCommitment>()
        val events = mutableListOf<String>()
        var failNextSave = false

        override fun save(commitment: PickupCommitment): Mono<PickupCommitment> {
            if (failNextSave) {
                failNextSave = false
                return Mono.error(IllegalStateException("database unavailable"))
            }
            rows[commitment.id] = commitment
            return Mono.just(commitment)
        }

        override fun saveWithEvent(
            commitment: PickupCommitment,
            eventType: String,
            payload: Map<String, Any>
        ): Mono<PickupCommitment> {
            if (failNextSave) {
                failNextSave = false
                return Mono.error(IllegalStateException("database unavailable"))
            }
            events += eventType
            rows[commitment.id] = commitment
            return Mono.just(commitment)
        }

        override fun find(id: UUID): Mono<PickupCommitment> =
            rows[id]?.let { Mono.just(it) }
                ?: Mono.error(NoSuchElementException("commitment not found"))
    }

    @Test
    fun holdStartsUnpaidAndKeepsCapacityThroughPickupGraceWindow() {
        val capacity = FakeCapacity()
        val repository = FakeRepository()
        val service = CommitmentService(capacity, repository)
        val pickupAt = Instant.now().plusSeconds(1800)

        val held = service.hold(HoldCommand("store-1", pickupAt, 2)).block()!!

        assertFalse(held.paymentAuthorized)
        assertEquals("lease-test", held.leaseToken)
        assertEquals(listOf("PickupSlotHeld"), repository.events)
        assertTrue(capacity.acquired.single() >= 1800)
    }

    @Test
    fun failedDatabaseWriteCompensatesRedisLease() {
        val capacity = FakeCapacity()
        val repository = FakeRepository().apply { failNextSave = true }
        val service = CommitmentService(capacity, repository)

        StepVerifier.create(
            service.hold(HoldCommand("store-1", Instant.now().plusSeconds(600), 1))
        )
            .expectErrorMatches { it.message == "database unavailable" }
            .verify()

        assertEquals(listOf("lease-test"), capacity.released)
    }

    @Test
    fun paymentAuthorizationIsSeparateFromHoldAndRequiredBeforeConfirm() {
        val capacity = FakeCapacity()
        val repository = FakeRepository()
        val service = CommitmentService(capacity, repository)
        val held = service.hold(
            HoldCommand("store-1", Instant.now().plusSeconds(600), 1)
        ).block()!!

        StepVerifier.create(service.confirm(held.id))
            .expectError(IllegalArgumentException::class.java)
            .verify()

        val paid = service.authorizePayment(held.id, "auth-123").block()!!
        val confirmed = service.confirm(held.id).block()!!

        assertTrue(paid.paymentAuthorized)
        assertEquals("CONFIRMED", confirmed.state.name)
        assertEquals(
            listOf("PickupSlotHeld", "PaymentAuthorized", "CommitmentConfirmed"),
            repository.events
        )
    }

    @Test
    fun pastPickupIsRejectedBeforeCapacityIsTouched() {
        val capacity = FakeCapacity()
        val service = CommitmentService(capacity, FakeRepository())

        assertThrows(IllegalArgumentException::class.java) {
            service.hold(HoldCommand("store-1", Instant.now().minusSeconds(1), 1))
        }
        assertTrue(capacity.acquired.isEmpty())
    }
}
