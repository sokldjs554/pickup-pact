package io.pickuppact.commitment.application

import io.pickuppact.commitment.domain.CommitmentState
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
        val releaseAttempts = mutableListOf<String>()
        val successfulReleases = mutableListOf<String>()
        var nextToken = "lease-test"
        var failNextRelease = false
        var currentAvailability = CapacityAvailability(capacityUnits = 40, reservedUnits = 0)

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
            releaseAttempts += token
            if (failNextRelease) {
                failNextRelease = false
                return Mono.error(IllegalStateException("redis unavailable"))
            }
            successfulReleases += token
            return Mono.empty()
        }

        override fun availability(storeId: String, pickupAt: Instant): Mono<CapacityAvailability> =
            Mono.just(currentAvailability)
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
    fun pickupSlotsAreFiveMinuteAlignedAndExposeCapacityFit() {
        val capacity = FakeCapacity().apply {
            currentAvailability = CapacityAvailability(capacityUnits = 4, reservedUnits = 3)
        }
        val service = CommitmentService(capacity, FakeRepository())
        val from = Instant.now().plusSeconds(611)

        val slots = service.pickupSlots("store-1", from, count = 3, units = 2)
            .collectList()
            .block()!!

        assertEquals(3, slots.size)
        assertTrue(slots.all { it.pickupAt.epochSecond % 300 == 0L })
        assertEquals(300L, slots[1].pickupAt.epochSecond - slots[0].pickupAt.epochSecond)
        assertEquals(4, slots[0].capacityUnits)
        assertEquals(3, slots[0].reservedUnits)
        assertEquals(1, slots[0].availableUnits)
        assertFalse(slots[0].canFit)
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
    fun failedDatabaseWriteCompensatesCapacityLease() {
        val capacity = FakeCapacity()
        val repository = FakeRepository().apply { failNextSave = true }
        val service = CommitmentService(capacity, repository)

        StepVerifier.create(
            service.hold(HoldCommand("store-1", Instant.now().plusSeconds(600), 1))
        )
            .expectErrorMatches { it.message == "database unavailable" }
            .verify()

        assertEquals(listOf("lease-test"), capacity.successfulReleases)
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
    fun claimPickupIsRetrySafeAndDoesNotDuplicateItsDomainEvent() {
        val capacity = FakeCapacity()
        val repository = FakeRepository()
        val service = CommitmentService(capacity, repository)
        val held = service.hold(
            HoldCommand("store-1", Instant.now().plusSeconds(600), 1)
        ).block()!!

        service.authorizePayment(held.id, "auth-pickup").block()
        service.confirm(held.id).block()

        val first = service.claimPickup(held.id).block()!!
        val retry = service.claimPickup(held.id).block()!!

        assertEquals(CommitmentState.PICKED_UP, first.state)
        assertEquals(CommitmentState.PICKED_UP, retry.state)
        assertEquals(1, repository.events.count { it == "PickupClaimed" })
        assertEquals(listOf("lease-test", "lease-test"), capacity.releaseAttempts)
    }

    @Test
    fun terminalStateCanRetryCapacityReleaseAfterExternalFailure() {
        val capacity = FakeCapacity().apply { failNextRelease = true }
        val repository = FakeRepository()
        val service = CommitmentService(capacity, repository)
        val held = service.hold(
            HoldCommand("store-1", Instant.now().plusSeconds(600), 1)
        ).block()!!

        service.authorizePayment(held.id, "auth-recovery").block()
        service.confirm(held.id).block()

        StepVerifier.create(service.claimPickup(held.id))
            .expectErrorMatches { it.message == "redis unavailable" }
            .verify()

        assertEquals(CommitmentState.PICKED_UP, repository.rows[held.id]!!.state)
        assertEquals(1, repository.events.count { it == "PickupClaimed" })

        val recovered = service.claimPickup(held.id).block()!!

        assertEquals(CommitmentState.PICKED_UP, recovered.state)
        assertEquals(2, capacity.releaseAttempts.size)
        assertEquals(listOf("lease-test"), capacity.successfulReleases)
        assertEquals(1, repository.events.count { it == "PickupClaimed" })
    }

    @Test
    fun cancellationRetryDoesNotDuplicateCancellationEvent() {
        val capacity = FakeCapacity()
        val repository = FakeRepository()
        val service = CommitmentService(capacity, repository)
        val held = service.hold(
            HoldCommand("store-1", Instant.now().plusSeconds(600), 1)
        ).block()!!

        val first = service.cancel(held.id).block()!!
        val retry = service.cancel(held.id).block()!!

        assertEquals(CommitmentState.CANCELLED, first.state)
        assertEquals(CommitmentState.CANCELLED, retry.state)
        assertEquals(1, repository.events.count { it == "CommitmentCancelled" })
        assertEquals(2, capacity.releaseAttempts.size)
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
