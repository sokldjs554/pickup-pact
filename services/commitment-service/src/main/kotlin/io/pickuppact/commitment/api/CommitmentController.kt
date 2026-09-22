package io.pickuppact.commitment.api

import com.fasterxml.jackson.annotation.JsonCreator
import com.fasterxml.jackson.annotation.JsonProperty
import io.pickuppact.commitment.application.CommitmentService
import io.pickuppact.commitment.application.HoldCommand
import io.pickuppact.commitment.domain.PickupCommitment
import io.pickuppact.commitment.domain.PromiseAdmissionInput
import io.pickuppact.commitment.domain.PromiseAdmissionQuote
import jakarta.validation.Valid
import jakarta.validation.constraints.Future
import jakarta.validation.constraints.Min
import jakarta.validation.constraints.NotBlank
import jakarta.validation.constraints.Positive
import org.springframework.web.bind.annotation.*
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

data class PromiseQuoteRequest(
    @field:Min(0) val backlogUnits: Int,
    @field:Min(1) val orderUnits: Int,
    @field:Positive val serviceRateUnitsPerMinute: Double,
    @field:Min(0) val travelMinutes: Int,
    @field:Min(1) val maxPromiseMinutes: Int = 18,
    @field:Min(0) val safetyMinutes: Int = 2
)

@RestController
@RequestMapping("/api/v1/commitments")
class CommitmentController(private val service: CommitmentService) {
    @PostMapping("/promise-quote")
    fun promiseQuote(@Valid @RequestBody request: PromiseQuoteRequest): PromiseAdmissionQuote =
        service.quotePromise(
            PromiseAdmissionInput(
                backlogUnits = request.backlogUnits,
                orderUnits = request.orderUnits,
                serviceRateUnitsPerMinute = request.serviceRateUnitsPerMinute,
                travelMinutes = request.travelMinutes,
                maxPromiseMinutes = request.maxPromiseMinutes,
                safetyMinutes = request.safetyMinutes
            )
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
