package io.pickuppact.commitment.application

import io.pickuppact.commitment.domain.PickupCommitment
import reactor.core.publisher.Mono
import java.time.Instant
import java.util.UUID

data class CapacityAvailability(
    val capacityUnits: Int,
    val reservedUnits: Int,
) {
    val availableUnits: Int
        get() = (capacityUnits - reservedUnits).coerceAtLeast(0)
}

interface CapacityLeasePort {
    fun acquire(storeId: String, pickupAt: Instant, units: Int, ttlSeconds: Long): Mono<String>

    /**
     * Release is deliberately idempotent. A terminal commitment may retry this
     * operation after the database transition has already committed.
     */
    fun release(token: String): Mono<Void>

    fun availability(storeId: String, pickupAt: Instant): Mono<CapacityAvailability>
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
