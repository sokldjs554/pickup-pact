package io.pickuppact.commitment.api

import io.pickuppact.commitment.application.CommitmentService
import io.pickuppact.commitment.application.HoldCommand
import io.pickuppact.commitment.domain.PickupCommitment
import org.springframework.web.bind.annotation.*
import reactor.core.publisher.Mono
import java.time.Instant
import java.util.UUID

data class HoldRequest(val storeId: String, val pickupAt: Instant, val units: Int, val paymentAuthorized: Boolean)

@RestController
@RequestMapping("/api/v1/commitments")
class CommitmentController(private val service: CommitmentService) {
    @PostMapping("/hold")
    fun hold(@RequestBody request: HoldRequest): Mono<PickupCommitment> =
        service.hold(HoldCommand(request.storeId, request.pickupAt, request.units, request.paymentAuthorized))

    @PostMapping("/{id}/confirm")
    fun confirm(@PathVariable id: UUID): Mono<PickupCommitment> = service.confirm(id)

    @PostMapping("/{id}/cancel")
    fun cancel(@PathVariable id: UUID): Mono<PickupCommitment> = service.cancel(id)
}
