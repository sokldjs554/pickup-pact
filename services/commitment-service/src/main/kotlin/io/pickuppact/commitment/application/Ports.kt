package io.pickuppact.commitment.application

import io.pickuppact.commitment.domain.PickupCommitment
import reactor.core.publisher.Mono
import java.time.Instant
import java.util.UUID

interface CapacityLeasePort {
    fun acquire(storeId: String, pickupAt: Instant, units: Int, ttlSeconds: Long): Mono<String>
    fun release(token: String): Mono<Void>
}

interface CommitmentRepository {
    fun save(commitment: PickupCommitment): Mono<PickupCommitment>
    fun find(id: UUID): Mono<PickupCommitment>

    fun saveWithEvent(
        commitment: PickupCommitment,
        eventType: String,
        payload: Map<String, Any>
    ): Mono<PickupCommitment> = save(commitment)
}
