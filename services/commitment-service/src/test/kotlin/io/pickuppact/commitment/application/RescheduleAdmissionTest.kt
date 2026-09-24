package io.pickuppact.commitment.application

import io.pickuppact.commitment.domain.CommitmentState
import io.pickuppact.commitment.domain.PickupCommitment
import io.pickuppact.commitment.domain.PickupPactPolicy
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test
import reactor.core.publisher.Mono
import reactor.test.StepVerifier
import java.time.Instant
import java.util.UUID

/** A rejected command must not reserve fresh capacity before domain validation. */
class RescheduleAdmissionTest {
    private class Capacity : CapacityLeasePort {
        var acquisitions = 0
        val released = mutableListOf<String>()
        override fun acquire(storeId: String, pickupAt: Instant, units: Int, ttlSeconds: Long): Mono<String> {
            acquisitions++
            return Mono.just("new-lease")
        }
        override fun release(token: String): Mono<Void> {
            released += token
            return Mono.empty()
        }
        override fun availability(storeId: String, pickupAt: Instant): Mono<CapacityAvailability> =
            Mono.just(CapacityAvailability(40, 0))
    }
    private class Repository(var row: PickupCommitment) : CommitmentRepository {
        var writes = 0
        override fun find(id: UUID): Mono<PickupCommitment> = Mono.just(row)
        override fun findByIdempotencyKey(idempotencyKey: String): Mono<PickupCommitment> = Mono.empty()
        override fun save(commitment: PickupCommitment): Mono<PickupCommitment> {
            writes++
            row = commitment
            return Mono.just(row)
        }
        override fun saveWithEvents(commitment: PickupCommitment, events: List<PendingDomainEvent>): Mono<PickupCommitment> = save(commitment)
    }
    private fun rejected(state: CommitmentState, pending: Boolean = false, missingPact: Boolean = false) {
        val at = Instant.now().plusSeconds(600)
        val initial = PickupCommitment(
            id = UUID.randomUUID(), storeId = "audit-store", pickupAt = at,
            units = 2, leaseToken = "old-lease", paymentAuthorized = true, state = state,
            pact = if (missingPact) null else PickupPactPolicy.issue(at),
            cancellationRequestId = if (pending) UUID.randomUUID() else null
        )
        val capacity = Capacity()
        val repository = Repository(initial)
        val service = CommitmentService(capacity, repository, QuoteTokenService("audit-secret"))
        StepVerifier.create(service.renegotiate(initial.id, at.plusSeconds(300)))
            .expectError(IllegalStateException::class.java).verify()
        assertEquals(0, capacity.acquisitions, "domain rejection must happen before reserving capacity")
        assertEquals(0, repository.writes)
        assertEquals(initial, repository.row)
    }
    @Test fun heldOrderCannotAcquireReplacementCapacity() = rejected(CommitmentState.HELD)
    @Test fun cancelledOrderCannotAcquireReplacementCapacity() = rejected(CommitmentState.CANCELLED)
    @Test fun pickedUpOrderCannotAcquireReplacementCapacity() = rejected(CommitmentState.PICKED_UP)
    @Test fun pendingCancellationCannotAcquireReplacementCapacity() = rejected(CommitmentState.CONFIRMED, pending = true)
    @Test fun missingPactCannotAcquireReplacementCapacity() = rejected(CommitmentState.CONFIRMED, missingPact = true)
}
