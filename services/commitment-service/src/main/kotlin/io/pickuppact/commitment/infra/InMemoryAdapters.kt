package io.pickuppact.commitment.infra

import io.pickuppact.commitment.application.CapacityLeasePort
import io.pickuppact.commitment.application.CommitmentRepository
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
    override fun save(commitment: PickupCommitment): Mono<PickupCommitment> {
        rows[commitment.id] = commitment
        return Mono.just(commitment)
    }
    override fun find(id: UUID): Mono<PickupCommitment> =
        rows[id]?.let { Mono.just(it) }
            ?: Mono.error(NoSuchElementException("commitment not found"))
}

@Component
@Profile("default", "test")
class InMemoryCapacityLease : CapacityLeasePort {
    override fun acquire(storeId: String, pickupAt: Instant, units: Int, ttlSeconds: Long): Mono<String> =
        Mono.just("lease-" + UUID.randomUUID())
    override fun release(token: String): Mono<Void> = Mono.empty()
}
