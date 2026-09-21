package io.pickuppact.commitment.application

import io.pickuppact.commitment.domain.CommitmentState
import io.pickuppact.commitment.domain.PickupCommitment
import org.springframework.stereotype.Service
import reactor.core.publisher.Mono
import java.time.Duration
import java.time.Instant
import java.util.UUID

data class HoldCommand(val storeId: String, val pickupAt: Instant, val units: Int, val paymentAuthorized: Boolean)

@Service
class CommitmentService(
    private val capacity: CapacityLeasePort,
    private val repository: CommitmentRepository,
    private val events: DomainEventPublisher
) {
    fun hold(command: HoldCommand): Mono<PickupCommitment> {
        val ttl = Duration.between(Instant.now(), command.pickupAt).seconds.coerceIn(30, 900)
        return capacity.acquire(command.storeId, command.pickupAt, command.units, ttl)
            .flatMap { token ->
                val commitment = PickupCommitment(
                    id = UUID.randomUUID(),
                    storeId = command.storeId,
                    pickupAt = command.pickupAt,
                    units = command.units,
                    leaseToken = token,
                    paymentAuthorized = command.paymentAuthorized,
                    state = CommitmentState.HELD
                )
                repository.save(commitment)
            }
    }

    fun confirm(id: UUID): Mono<PickupCommitment> =
        repository.find(id)
            .map { it.confirm(Instant.now()) }
            .flatMap(repository::save)
            .flatMap { saved ->
                events.publish("PICKUP_CONFIRMED", saved.id, mapOf("pickupAt" to saved.pickupAt.toString(), "units" to saved.units))
                    .thenReturn(saved)
            }

    fun cancel(id: UUID): Mono<PickupCommitment> =
        repository.find(id)
            .map { it.cancel() }
            .flatMap(repository::save)
            .flatMap { saved ->
                events.publish("PICKUP_CANCELLED", saved.id, mapOf("occurredAt" to Instant.now().toString()))
                    .then(capacity.release(saved.leaseToken))
                    .thenReturn(saved)
            }
}
