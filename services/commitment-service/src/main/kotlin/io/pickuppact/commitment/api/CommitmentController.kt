package io.pickuppact.commitment.api

import com.fasterxml.jackson.annotation.JsonCreator
import com.fasterxml.jackson.annotation.JsonProperty
import io.pickuppact.commitment.application.CommitmentService
import io.pickuppact.commitment.application.HoldCommand
import io.pickuppact.commitment.application.PickupQuote
import io.pickuppact.commitment.application.PickupSlotOption
import io.pickuppact.commitment.domain.PickupCommitment
import io.pickuppact.commitment.domain.PickupItem
import jakarta.validation.Valid
import jakarta.validation.constraints.Future
import jakarta.validation.constraints.Max
import jakarta.validation.constraints.Min
import jakarta.validation.constraints.NotBlank
import jakarta.validation.constraints.Size
import org.springframework.web.bind.annotation.*
import reactor.core.publisher.Flux
import reactor.core.publisher.Mono
import java.time.Instant
import java.util.UUID

data class PickupItemRequest(
    @field:NotBlank val sku: String,
    @field:Min(1) @field:Max(20) val quantity: Int
)

data class QuoteRequest(
    @field:NotBlank val storeId: String,
    @field:Size(min = 1, max = 20) @field:Valid val items: List<PickupItemRequest>,
    val from: Instant? = null,
    @field:Min(1) @field:Max(12) val count: Int = 6
)

data class HoldRequest(
    @field:NotBlank val quoteToken: String,
    @field:Future val pickupAt: Instant
)

data class PaymentAuthorizationRequest @JsonCreator(mode = JsonCreator.Mode.PROPERTIES) constructor(
    @field:NotBlank @JsonProperty("authorizationId") val authorizationId: String
)

data class PickupRescheduleRequest(
    @field:Future val pickupAt: Instant
)

@RestController
@RequestMapping("/api/v1/commitments")
class CommitmentController(private val service: CommitmentService) {
    @PostMapping("/quotes")
    fun quote(@Valid @RequestBody request: QuoteRequest): Mono<PickupQuote> =
        service.quote(
            storeId = request.storeId,
            items = request.items.map { PickupItem(it.sku, it.quantity) },
            from = request.from ?: Instant.now().plusSeconds(300),
            count = request.count
        )

    /**
     * Diagnostic endpoint. Customer admission uses POST /quotes so workload is
     * computed by the server rather than trusted from a client-supplied number.
     */
    @GetMapping("/slots")
    fun slots(
        @RequestParam storeId: String,
        @RequestParam(required = false) from: Instant?,
        @RequestParam(defaultValue = "6") count: Int,
        @RequestParam(defaultValue = "1") units: Int,
    ): Flux<PickupSlotOption> =
        service.pickupSlots(
            storeId = storeId,
            from = from ?: Instant.now().plusSeconds(300),
            count = count,
            units = units,
        )

    @PostMapping("/hold")
    fun hold(
        @RequestHeader("Idempotency-Key") idempotencyKey: String,
        @Valid @RequestBody request: HoldRequest
    ): Mono<PickupCommitment> =
        service.hold(HoldCommand(request.quoteToken, request.pickupAt, idempotencyKey))

    @GetMapping("/{id}")
    fun get(@PathVariable id: UUID): Mono<PickupCommitment> = service.get(id)

    @PostMapping("/{id}/authorize-payment")
    fun authorizePayment(
        @PathVariable id: UUID,
        @Valid @RequestBody request: PaymentAuthorizationRequest
    ): Mono<PickupCommitment> =
        service.authorizePayment(id, request.authorizationId)

    @PostMapping("/{id}/confirm")
    fun confirm(@PathVariable id: UUID): Mono<PickupCommitment> = service.confirm(id)

    @PostMapping("/{id}/reschedule")
    fun reschedule(
        @PathVariable id: UUID,
        @Valid @RequestBody request: PickupRescheduleRequest
    ): Mono<PickupCommitment> =
        service.renegotiate(id, request.pickupAt)

    @PostMapping("/{id}/breach-pact")
    fun breachPact(@PathVariable id: UUID): Mono<PickupCommitment> =
        service.breachPact(id)

    @PostMapping("/{id}/claim-pickup")
    fun claimPickup(@PathVariable id: UUID): Mono<PickupCommitment> = service.claimPickup(id)

    @PostMapping("/{id}/cancel")
    fun cancel(@PathVariable id: UUID): Mono<PickupCommitment> = service.cancel(id)
}
