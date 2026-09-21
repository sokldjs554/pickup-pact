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
    private val repository: CommitmentRepository
) {
    fun hold(command: HoldCommand): Mono<PickupCommitment> {
        val ttl = Duration.between(Instant.now(), command.pickupAt).seconds.coerceIn(30, 900)
        return capacity.acquire(command.storeId, command.pickupAt, command.units, ttl)
            .flatMap { token ->
                repository.save(
                    PickupCommitment(
                        id = UUID.randomUUID(),
                        storeId = command.storeId,
                        pickupAt = command.pickupAt,
                        units = command.units,
                        leaseToken = token,
                        paymentAuthorized = command.paymentAuthorized,
                        state = CommitmentState.HELD
                    )
                )
            }
    }

    fun confirm(id: UUID): Mono<PickupCommitment> =
        repository.find(id)
            .map { it.confirm(Instant.now()) }
            .flatMap { saved ->
                repository.saveWithEvent(
                    saved,
                    "PICKUP_CONFIRMED",
                    mapOf("pickupAt" to saved.pickupAt.toString(), "units" to saved.units)
                )
            }

    fun cancel(id: UUID): Mono<PickupCommitment> =
        repository.find(id)
            .map { it.cancel() }
            .flatMap { saved ->
                repository.saveWithEvent(
                    saved,
                    "PICKUP_CANCELLED",
                    mapOf("occurredAt" to Instant.now().toString())
                )
            }
            .flatMap { saved -> capacity.release(saved.leaseToken).thenReturn(saved) }
}
