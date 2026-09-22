package io.pickuppact.commitment.api

import com.fasterxml.jackson.annotation.JsonCreator
import com.fasterxml.jackson.annotation.JsonProperty
import io.pickuppact.commitment.application.CommitmentService
import io.pickuppact.commitment.application.HoldCommand
import io.pickuppact.commitment.application.PickupSlotOption
import io.pickuppact.commitment.domain.PickupCommitment
import jakarta.validation.Valid
import jakarta.validation.constraints.Future
import jakarta.validation.constraints.Min
import jakarta.validation.constraints.NotBlank
import org.springframework.web.bind.annotation.*
import reactor.core.publisher.Flux
import reactor.core.publisher.Mono
import java.time.Instant
import java.util.UUID

data class HoldRequest(
    @field:NotBlank val storeId: String,
    @field:Future val pickupAt: Instant,
    @field:Min(1) val units: Int
)

data class PaymentAuthorizationRequest @JsonCreator(mode = JsonCreator.Mode.PROPERTIES) constructor(
    @field:NotBlank @JsonProperty("authorizationId") val authorizationId: String
)

@RestController
@RequestMapping("/api/v1/commitments")
class CommitmentController(private val service: CommitmentService) {
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
    fun hold(@Valid @RequestBody request: HoldRequest): Mono<PickupCommitment> =
        service.hold(HoldCommand(request.storeId, request.pickupAt, request.units))

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

    @PostMapping("/{id}/claim-pickup")
    fun claimPickup(@PathVariable id: UUID): Mono<PickupCommitment> = service.claimPickup(id)

    @PostMapping("/{id}/cancel")
    fun cancel(@PathVariable id: UUID): Mono<PickupCommitment> = service.cancel(id)
}
