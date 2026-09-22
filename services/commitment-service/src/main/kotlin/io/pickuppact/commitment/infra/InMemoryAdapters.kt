package io.pickuppact.commitment.infra

import io.pickuppact.commitment.application.CapacityAvailability
import io.pickuppact.commitment.application.CapacityLeasePort
import io.pickuppact.commitment.application.CommitmentRepository
import io.pickuppact.commitment.application.PendingDomainEvent
import io.pickuppact.commitment.domain.PickupCommitment
import org.springframework.context.annotation.Profile
import org.springframework.stereotype.Component
import reactor.core.publisher.Mono
import java.time.Instant
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap

@Component
@Profile("default", "test")
class InMemoryCommitmentRepository : CommitmentRepository {
    private val rows = ConcurrentHashMap<UUID, PickupCommitment>()
    private val idempotencyIndex = ConcurrentHashMap<String, UUID>()

    override fun save(commitment: PickupCommitment): Mono<PickupCommitment> {
        val key = commitment.idempotencyKey
        if (key.isNotBlank()) {
            val existing = idempotencyIndex.putIfAbsent(key, commitment.id)
            if (existing != null && existing != commitment.id) {
                return Mono.error(IllegalStateException("idempotency key already belongs to another commitment"))
            }
        }
        rows[commitment.id] = commitment
        return Mono.just(commitment)
    }

    override fun saveWithEvents(
        commitment: PickupCommitment,
        events: List<PendingDomainEvent>
    ): Mono<PickupCommitment> = save(commitment)

    override fun find(id: UUID): Mono<PickupCommitment> =
        rows[id]?.let { Mono.just(it) }
            ?: Mono.error(NoSuchElementException("commitment not found"))

    override fun findByIdempotencyKey(idempotencyKey: String): Mono<PickupCommitment> =
        idempotencyIndex[idempotencyKey]
            ?.let { rows[it] }
            ?.let { Mono.just(it) }
            ?: Mono.empty()
}

@Component
@Profile("default", "test")
class InMemoryCapacityLease : CapacityLeasePort {
    override fun acquire(storeId: String, pickupAt: Instant, units: Int, ttlSeconds: Long): Mono<String> =
        Mono.just("lease-" + UUID.randomUUID())

    override fun release(token: String): Mono<Void> = Mono.empty()

    override fun availability(storeId: String, pickupAt: Instant): Mono<CapacityAvailability> =
        Mono.just(CapacityAvailability(capacityUnits = 40, reservedUnits = 0))
}
