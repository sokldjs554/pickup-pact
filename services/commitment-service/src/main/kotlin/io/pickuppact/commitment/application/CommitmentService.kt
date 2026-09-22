package io.pickuppact.commitment.application

import io.pickuppact.commitment.domain.CommitmentState
import io.pickuppact.commitment.domain.MenuWorkloadPolicy
import io.pickuppact.commitment.domain.PickupCommitment
import io.pickuppact.commitment.domain.PickupItem
import org.springframework.dao.DataIntegrityViolationException
import org.springframework.stereotype.Service
import reactor.core.publisher.Flux
import reactor.core.publisher.Mono
import java.nio.charset.StandardCharsets
import java.security.MessageDigest
import java.time.Duration
import java.time.Instant
import java.util.HexFormat
import java.util.UUID

data class HoldCommand(
    val quoteToken: String,
    val pickupAt: Instant,
    val idempotencyKey: String
)

data class PickupQuote(
    val quoteToken: String,
    val storeId: String,
    val units: Int,
    val expiresAt: Instant,
    val slots: List<PickupSlotOption>
)

data class PickupSlotOption(
    val pickupAt: Instant,
    val capacityUnits: Int,
    val reservedUnits: Int,
    val availableUnits: Int,
    val requestedUnits: Int,
    val canFit: Boolean,
)

@Service
class CommitmentService(
    private val capacity: CapacityLeasePort,
    private val repository: CommitmentRepository,
    private val quoteTokens: QuoteTokenService
) {
    fun quote(
        storeId: String,
        items: List<PickupItem>,
        from: Instant,
        count: Int
    ): Mono<PickupQuote> {
        require(storeId.isNotBlank()) { "storeId must not be blank" }
        val units = MenuWorkloadPolicy.units(items)
        val now = Instant.now()
        val expiresAt = now.plus(QUOTE_TTL)
        val token = quoteTokens.issue(storeId, units, expiresAt)

        return pickupSlots(storeId, from, count, units)
            .collectList()
            .map { slots ->
                PickupQuote(
                    quoteToken = token,
                    storeId = storeId,
                    units = units,
                    expiresAt = expiresAt,
                    slots = slots
                )
            }
    }

    /**
     * Low-level availability query retained for operator diagnostics and tests.
     * Customer admission must use [quote] + signed quoteToken.
     */
    fun pickupSlots(
        storeId: String,
        from: Instant,
        count: Int,
        units: Int
    ): Flux<PickupSlotOption> {
        require(storeId.isNotBlank()) { "storeId must not be blank" }
        require(count in 1..12) { "count must be between 1 and 12" }
        require(units > 0) { "units must be positive" }

        val now = Instant.now()
        val seed = if (from.isAfter(now)) from else now.plusSeconds(SLOT_SECONDS)
        val firstSlot = alignToSlot(seed)

        return Flux.range(0, count)
            .concatMap { offset ->
                val pickupAt = firstSlot.plusSeconds(SLOT_SECONDS * offset.toLong())
                capacity.availability(storeId, pickupAt)
                    .map { availability ->
                        PickupSlotOption(
                            pickupAt = pickupAt,
                            capacityUnits = availability.capacityUnits,
                            reservedUnits = availability.reservedUnits,
                            availableUnits = availability.availableUnits,
                            requestedUnits = units,
                            canFit = availability.availableUnits >= units,
                        )
                    }
            }
    }

    fun hold(command: HoldCommand): Mono<PickupCommitment> {
        require(command.idempotencyKey.isNotBlank()) { "Idempotency-Key is required" }
        val now = Instant.now()
        require(command.pickupAt.isAfter(now)) { "pickup time must be in the future" }

        val quote = quoteTokens.verify(command.quoteToken, now)
        val fingerprint = requestFingerprint(command.quoteToken, command.pickupAt)

        return repository.findByIdempotencyKey(command.idempotencyKey)
            .flatMap { existing ->
                if (existing.requestFingerprint == fingerprint) Mono.just(existing)
                else Mono.error(IllegalStateException("idempotency key reused with a different hold request"))
            }
            .switchIfEmpty(
                Mono.defer {
                    val ttl = (Duration.between(now, command.pickupAt) + Duration.ofMinutes(5))
                        .seconds.coerceAtLeast(30)
                    capacity.acquire(quote.storeId, command.pickupAt, quote.units, ttl)
                        .flatMap { token ->
                            val held = PickupCommitment(
                                id = UUID.randomUUID(),
                                storeId = quote.storeId,
                                pickupAt = command.pickupAt,
                                units = quote.units,
                                leaseToken = token,
                                paymentAuthorized = false,
                                state = CommitmentState.HELD,
                                idempotencyKey = command.idempotencyKey,
                                requestFingerprint = fingerprint
                            )
                            repository.saveWithEvent(
                                held,
                                "PickupSlotHeld",
                                mapOf(
                                    "store_id" to held.storeId,
                                    "pickup_at" to held.pickupAt.toString(),
                                    "capacity_units" to held.units
                                )
                            ).onErrorResume(DataIntegrityViolationException::class.java) {
                                capacity.release(token)
                                    .onErrorResume { Mono.empty() }
                                    .then(repository.findByIdempotencyKey(command.idempotencyKey))
                                    .flatMap { existing ->
                                        if (existing.requestFingerprint == fingerprint) Mono.just(existing)
                                        else Mono.error(IllegalStateException("idempotency key conflict"))
                                    }
                            }.onErrorResume { error ->
                                capacity.release(token)
                                    .onErrorResume { Mono.empty() }
                                    .then(Mono.error(error))
                            }
                        }
                }
            )
    }

    fun get(id: UUID): Mono<PickupCommitment> = repository.find(id)

    fun authorizePayment(id: UUID, authorizationId: String): Mono<PickupCommitment> =
        repository.find(id)
            .map { it.authorizePayment() }
            .flatMap { saved ->
                repository.saveWithEvent(
                    saved,
                    "PaymentAuthorized",
                    mapOf("authorization_id" to authorizationId)
                )
            }

    fun confirm(id: UUID): Mono<PickupCommitment> =
        repository.find(id)
            .map { it.confirm(Instant.now()) }
            .flatMap { saved ->
                val pact = checkNotNull(saved.pact)
                repository.saveWithEvents(
                    saved,
                    listOf(
                        PendingDomainEvent(
                            "CommitmentConfirmed",
                            mapOf(
                                "pickup_at" to saved.pickupAt.toString(),
                                "capacity_units" to saved.units
                            )
                        ),
                        PendingDomainEvent(
                            "PickupPactIssued",
                            mapOf(
                                "promised_at" to pact.promisedAt.toString(),
                                "latest_at" to pact.latestAt.toString(),
                                "compensation_points" to pact.compensationPoints,
                                "version" to pact.version
                            )
                        )
                    )
                )
            }

    fun renegotiate(id: UUID, newPickupAt: Instant): Mono<PickupCommitment> {
        val now = Instant.now()
        require(newPickupAt.isAfter(now)) { "new pickup time must be in the future" }

        return repository.find(id).flatMap { current ->
            if (current.pickupAt == newPickupAt && current.state == CommitmentState.CONFIRMED) {
                return@flatMap Mono.just(current)
            }
            val ttl = (Duration.between(now, newPickupAt) + Duration.ofMinutes(5))
                .seconds.coerceAtLeast(30)
            capacity.acquire(current.storeId, newPickupAt, current.units, ttl)
                .flatMap { newLease ->
                    val previousPickupAt = current.pickupAt
                    val previousLease = current.leaseToken
                    val updated = current.renegotiate(newPickupAt, newLease)
                    val pact = checkNotNull(updated.pact)
                    repository.saveWithEvents(
                        updated,
                        listOf(
                            PendingDomainEvent(
                                "PickupRescheduled",
                                mapOf(
                                    "from_pickup_at" to previousPickupAt.toString(),
                                    "pickup_at" to newPickupAt.toString(),
                                    "capacity_units" to updated.units
                                )
                            ),
                            PendingDomainEvent(
                                "PickupPactRenegotiated",
                                mapOf(
                                    "promised_at" to pact.promisedAt.toString(),
                                    "latest_at" to pact.latestAt.toString(),
                                    "compensation_points" to pact.compensationPoints,
                                    "version" to pact.version
                                )
                            )
                        )
                    )
                        .onErrorResume { error ->
                            capacity.release(newLease)
                                .onErrorResume { Mono.empty() }
                                .then(Mono.error(error))
                        }
                        .flatMap { saved ->
                            // Failure to release the old lease is conservative: it can
                            // temporarily under-admit capacity, but cannot overbook.
                            capacity.release(previousLease)
                                .onErrorResume { Mono.empty() }
                                .thenReturn(saved)
                        }
                }
        }
    }

    fun breachPact(id: UUID): Mono<PickupCommitment> =
        repository.find(id)
            .map { it.breachPact() }
            .flatMap { breached ->
                val pact = checkNotNull(breached.pact)
                repository.saveWithEvent(
                    breached,
                    "PickupPactBreached",
                    mapOf(
                        "promised_at" to pact.promisedAt.toString(),
                        "latest_at" to pact.latestAt.toString(),
                        "compensation_points" to pact.compensationPoints,
                        "version" to pact.version
                    )
                )
            }

    fun claimPickup(id: UUID): Mono<PickupCommitment> =
        repository.find(id)
            .flatMap { current ->
                if (current.state == CommitmentState.PICKED_UP) {
                    capacity.release(current.leaseToken).thenReturn(current)
                } else {
                    val pickedUp = current.claimPickup()
                    repository.saveWithEvent(
                        pickedUp,
                        "PickupClaimed",
                        mapOf("pickup_at" to pickedUp.pickupAt.toString())
                    ).flatMap { saved ->
                        capacity.release(saved.leaseToken).thenReturn(saved)
                    }
                }
            }

    fun cancel(id: UUID): Mono<PickupCommitment> =
        repository.find(id)
            .flatMap { current ->
                if (current.state == CommitmentState.CANCELLED) {
                    capacity.release(current.leaseToken).thenReturn(current)
                } else {
                    val cancelled = current.cancel()
                    repository.saveWithEvent(
                        cancelled,
                        "CommitmentCancelled",
                        mapOf("reason" to "customer_request")
                    ).flatMap { saved ->
                        capacity.release(saved.leaseToken).thenReturn(saved)
                    }
                }
            }

    private fun requestFingerprint(quoteToken: String, pickupAt: Instant): String {
        val bytes = MessageDigest.getInstance("SHA-256")
            .digest("$quoteToken|${pickupAt}".toByteArray(StandardCharsets.UTF_8))
        return HexFormat.of().formatHex(bytes)
    }

    private fun alignToSlot(value: Instant): Instant {
        val epoch = value.epochSecond
        val aligned = ((epoch + SLOT_SECONDS - 1) / SLOT_SECONDS) * SLOT_SECONDS
        return Instant.ofEpochSecond(aligned)
    }

    private companion object {
        const val SLOT_SECONDS = 300L
        val QUOTE_TTL: Duration = Duration.ofMinutes(2)
    }
}
