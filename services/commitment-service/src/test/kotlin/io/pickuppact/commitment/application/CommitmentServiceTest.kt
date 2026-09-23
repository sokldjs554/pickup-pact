package io.pickuppact.commitment.application

import io.pickuppact.commitment.domain.CommitmentState
import io.pickuppact.commitment.domain.PickupCommitment
import io.pickuppact.commitment.domain.PickupItem
import io.pickuppact.commitment.domain.PickupPactStatus
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import reactor.core.publisher.Mono
import reactor.test.StepVerifier
import java.time.Instant
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap

class CommitmentServiceTest {
    private class FakeCapacity : CapacityLeasePort {
        val acquiredUnits = mutableListOf<Int>()
        val releaseAttempts = mutableListOf<String>()
        val successfulReleases = mutableListOf<String>()
        var nextToken = "lease-test"
        var failNextRelease = false
        var currentAvailability = CapacityAvailability(capacityUnits = 40, reservedUnits = 0)

        override fun acquire(
            storeId: String,
            pickupAt: Instant,
            units: Int,
            ttlSeconds: Long
        ): Mono<String> {
            acquiredUnits += units
            return Mono.just(nextToken)
        }

        override fun release(token: String): Mono<Void> {
            releaseAttempts += token
            if (failNextRelease) {
                failNextRelease = false
                return Mono.error(IllegalStateException("redis unavailable"))
            }
            successfulReleases += token
            return Mono.empty()
        }

        override fun availability(storeId: String, pickupAt: Instant): Mono<CapacityAvailability> =
            Mono.just(currentAvailability)
    }

    private class FakeRepository : CommitmentRepository {
        val rows = ConcurrentHashMap<UUID, PickupCommitment>()
        val byIdempotency = ConcurrentHashMap<String, UUID>()
        val events = mutableListOf<String>()
        var failNextSave = false

        override fun save(commitment: PickupCommitment): Mono<PickupCommitment> {
            if (failNextSave) {
                failNextSave = false
                return Mono.error(IllegalStateException("database unavailable"))
            }
            rows[commitment.id] = commitment
            if (commitment.idempotencyKey.isNotBlank()) {
                byIdempotency[commitment.idempotencyKey] = commitment.id
            }
            return Mono.just(commitment)
        }

        override fun saveWithEvents(
            commitment: PickupCommitment,
            events: List<PendingDomainEvent>
        ): Mono<PickupCommitment> {
            if (failNextSave) {
                failNextSave = false
                return Mono.error(IllegalStateException("database unavailable"))
            }
            this.events += events.map { it.eventType }
            return save(commitment)
        }

        override fun find(id: UUID): Mono<PickupCommitment> =
            rows[id]?.let { Mono.just(it) }
                ?: Mono.error(NoSuchElementException("commitment not found"))

        override fun findByIdempotencyKey(idempotencyKey: String): Mono<PickupCommitment> =
            byIdempotency[idempotencyKey]
                ?.let { rows[it] }
                ?.let { Mono.just(it) }
                ?: Mono.empty()
    }

    private val quoteTokens = QuoteTokenService("test-secret")
    private fun service(capacity: FakeCapacity = FakeCapacity(), repository: FakeRepository = FakeRepository()) =
        CommitmentService(capacity, repository, quoteTokens)

    private fun quoteToken(
        storeId: String = "store-1",
        units: Int = 2,
        totalAmount: Int = 9000
    ): String =
        quoteTokens.issue(storeId, units, totalAmount, Instant.now().plusSeconds(120))

    @Test
    fun quoteComputesWorkloadOnServerFromMenuItems() {
        val capacity = FakeCapacity()
        val service = service(capacity)

        val quote = service.quote(
            storeId = "store-1",
            items = listOf(PickupItem("americano", 2), PickupItem("cafe-latte", 1)),
            from = Instant.now().plusSeconds(600),
            count = 2
        ).block()!!

        assertEquals(4, quote.units)
        assertEquals(14000, quote.totalAmount)
        assertEquals(2, quote.slots.size)
        assertEquals(listOf(4, 4), quote.slots.map { it.requestedUnits })
        assertEquals("store-1", quoteTokens.verify(quote.quoteToken).storeId)
        assertEquals(14000, quoteTokens.verify(quote.quoteToken).totalAmount)
    }

    @Test
    fun unknownMenuSkuIsRejectedBeforeCapacityIsTouched() {
        val capacity = FakeCapacity()
        val service = service(capacity)

        assertThrows(IllegalArgumentException::class.java) {
            service.quote(
                "store-1",
                listOf(PickupItem("not-a-menu", 1)),
                Instant.now().plusSeconds(600),
                1
            )
        }
        assertTrue(capacity.acquiredUnits.isEmpty())
    }

    @Test
    fun holdUsesSignedQuoteAndIsIdempotentByKey() {
        val capacity = FakeCapacity()
        val repository = FakeRepository()
        val service = service(capacity, repository)
        val pickupAt = Instant.now().plusSeconds(900)
        val command = HoldCommand(quoteToken(units = 3), pickupAt, "idem-1")

        val first = service.hold(command).block()!!
        val retry = service.hold(command).block()!!

        assertEquals(first.id, retry.id)
        assertEquals(3, first.units)
        assertEquals(9000, first.totalAmount)
        assertEquals(listOf(3), capacity.acquiredUnits)
        assertEquals(1, repository.events.count { it == "PickupSlotHeld" })
    }

    @Test
    fun idempotencyKeyCannotBeReusedForDifferentHoldRequest() {
        val service = service()
        val token = quoteToken()

        service.hold(HoldCommand(token, Instant.now().plusSeconds(900), "idem-reuse")).block()

        StepVerifier.create(
            service.hold(HoldCommand(token, Instant.now().plusSeconds(1200), "idem-reuse"))
        )
            .expectError(IllegalStateException::class.java)
            .verify()
    }

    @Test
    fun failedDatabaseWriteCompensatesCapacityLease() {
        val capacity = FakeCapacity()
        val repository = FakeRepository().apply { failNextSave = true }
        val service = service(capacity, repository)

        StepVerifier.create(
            service.hold(
                HoldCommand(
                    quoteToken(units = 1),
                    Instant.now().plusSeconds(600),
                    "idem-db-fail"
                )
            )
        )
            .expectErrorMatches { it.message == "database unavailable" }
            .verify()

        assertEquals(listOf("lease-test"), capacity.successfulReleases)
    }

    @Test
    fun paymentAuthorizationIsSeparateAndConfirmIssuesPickupPactAtomically() {
        val repository = FakeRepository()
        val service = service(repository = repository)
        val held = service.hold(
            HoldCommand(quoteToken(units = 1), Instant.now().plusSeconds(600), "idem-confirm")
        ).block()!!

        StepVerifier.create(service.confirm(held.id))
            .expectError(IllegalStateException::class.java)
            .verify()

        service.authorizePayment(held.id, "auth-123").block()
        val confirmed = service.confirm(held.id).block()!!

        assertTrue(confirmed.paymentAuthorized)
        assertEquals(CommitmentState.CONFIRMED, confirmed.state)
        assertEquals(PickupPactStatus.ACTIVE, confirmed.pact!!.status)
        assertEquals(
            listOf("PickupSlotHeld", "PaymentAuthorized", "CommitmentConfirmed", "PickupPactIssued"),
            repository.events
        )
    }

    @Test
    fun acceptedRescheduleVersionsPactAndMovesCapacityLease() {
        val capacity = FakeCapacity()
        val repository = FakeRepository()
        val service = service(capacity, repository)
        val held = service.hold(
            HoldCommand(quoteToken(units = 2), Instant.now().plusSeconds(600), "idem-reslot")
        ).block()!!
        service.authorizePayment(held.id, "auth-reslot").block()
        val confirmed = service.confirm(held.id).block()!!

        capacity.nextToken = "lease-rescheduled"
        val newPickupAt = Instant.now().plusSeconds(1200)
        val updated = service.renegotiate(held.id, newPickupAt).block()!!

        assertEquals(newPickupAt, updated.pickupAt)
        assertEquals("lease-rescheduled", updated.leaseToken)
        assertEquals(2, updated.pact!!.version)
        assertEquals(listOf(2, 2), capacity.acquiredUnits)
        assertTrue(capacity.releaseAttempts.contains(confirmed.leaseToken))
        assertTrue(repository.events.contains("PickupRescheduled"))
        assertTrue(repository.events.contains("PickupPactRenegotiated"))
    }

    @Test
    fun pactBreachBeforeDeadlineIsRejectedWithoutRewardEvidence() {
        val repository = FakeRepository()
        val service = service(repository = repository)
        val held = service.hold(
            HoldCommand(quoteToken(units = 1), Instant.now().plusSeconds(600), "idem-breach-early")
        ).block()!!
        service.authorizePayment(held.id, "auth-breach-early").block()
        service.confirm(held.id).block()

        StepVerifier.create(service.breachPact(held.id))
            .expectError(IllegalStateException::class.java)
            .verify()

        assertEquals(0, repository.events.count { it == "PickupPactBreached" })
        assertEquals(PickupPactStatus.ACTIVE, repository.rows[held.id]!!.pact!!.status)
    }

    @Test
    fun pactBreachAfterDeadlineIsPersistedOnceAsDomainEvidence() {
        val repository = FakeRepository()
        val service = service(repository = repository)
        val held = service.hold(
            HoldCommand(quoteToken(units = 1), Instant.now().plusSeconds(600), "idem-breach")
        ).block()!!
        service.authorizePayment(held.id, "auth-breach").block()
        val confirmed = service.confirm(held.id).block()!!

        val now = Instant.now()
        repository.rows[held.id] = confirmed.copy(
            pact = confirmed.pact!!.copy(
                promisedAt = now.minusSeconds(600),
                latestAt = now.minusSeconds(300)
            )
        )

        val breached = service.breachPact(held.id).block()!!

        val breachedPact = breached.pact!!
        assertEquals(PickupPactStatus.COMPENSATED, breachedPact.status)
        assertTrue(breachedPact.compensationGranted)
        assertEquals(1, repository.events.count { it == "PickupPactBreached" })

        StepVerifier.create(service.breachPact(held.id))
            .expectError(IllegalStateException::class.java)
            .verify()
    }

    @Test
    fun duplicateLateFulfillmentSignalDoesNotDoubleCompensate() {
        val repository = FakeRepository()
        val service = service(repository = repository)
        val held = service.hold(
            HoldCommand(quoteToken(units = 1), Instant.now().plusSeconds(600), "idem-fulfillment-breach")
        ).block()!!
        service.authorizePayment(held.id, "auth-fulfillment-breach").block()
        val confirmed = service.confirm(held.id).block()!!

        val observedAt = Instant.now()
        repository.rows[held.id] = confirmed.copy(
            pact = confirmed.pact!!.copy(
                promisedAt = observedAt.minusSeconds(600),
                latestAt = observedAt.minusSeconds(300)
            )
        )

        val first = service.breachPactFromFulfillment(held.id, observedAt).block()!!
        val duplicate = service.breachPactFromFulfillment(held.id, observedAt.plusSeconds(1)).block()!!

        assertEquals(PickupPactStatus.COMPENSATED, first.pact!!.status)
        assertEquals(PickupPactStatus.COMPENSATED, duplicate.pact!!.status)
        assertEquals(1, repository.events.count { it == "PickupPactBreached" })
    }

    @Test
    fun claimPickupIsRetrySafeAndDoesNotDuplicateItsDomainEvent() {
        val capacity = FakeCapacity()
        val repository = FakeRepository()
        val service = service(capacity, repository)
        val held = service.hold(
            HoldCommand(quoteToken(units = 1), Instant.now().plusSeconds(600), "idem-claim")
        ).block()!!

        service.authorizePayment(held.id, "auth-pickup").block()
        service.confirm(held.id).block()

        val first = service.claimPickup(held.id).block()!!
        val retry = service.claimPickup(held.id).block()!!

        assertEquals(CommitmentState.PICKED_UP, first.state)
        assertEquals(CommitmentState.PICKED_UP, retry.state)
        assertEquals(1, repository.events.count { it == "PickupClaimed" })
        assertEquals(listOf("lease-test", "lease-test"), capacity.releaseAttempts)
    }

    @Test
    fun terminalStateCanRetryCapacityReleaseAfterExternalFailure() {
        val capacity = FakeCapacity().apply { failNextRelease = true }
        val repository = FakeRepository()
        val service = service(capacity, repository)
        val held = service.hold(
            HoldCommand(quoteToken(units = 1), Instant.now().plusSeconds(600), "idem-release-retry")
        ).block()!!

        service.authorizePayment(held.id, "auth-recovery").block()
        service.confirm(held.id).block()

        StepVerifier.create(service.claimPickup(held.id))
            .expectErrorMatches { it.message == "redis unavailable" }
            .verify()

        assertEquals(CommitmentState.PICKED_UP, repository.rows[held.id]!!.state)
        assertEquals(1, repository.events.count { it == "PickupClaimed" })

        val recovered = service.claimPickup(held.id).block()!!

        assertEquals(CommitmentState.PICKED_UP, recovered.state)
        assertEquals(2, capacity.releaseAttempts.size)
        assertEquals(listOf("lease-test"), capacity.successfulReleases)
        assertEquals(1, repository.events.count { it == "PickupClaimed" })
    }

    @Test
    fun confirmedCancellationWaitsForMerchantApprovalBeforeRelease() {
        val capacity = FakeCapacity()
        val repository = FakeRepository()
        val service = service(capacity, repository)
        val held = service.hold(
            HoldCommand(quoteToken(units = 1), Instant.now().plusSeconds(600), "idem-confirmed-cancel")
        ).block()!!
        service.authorizePayment(held.id, "auth-confirmed-cancel").block()
        service.confirm(held.id).block()

        val pending = service.cancel(held.id).block()!!
        val retry = service.cancel(held.id).block()!!

        assertEquals(CommitmentState.CONFIRMED, pending.state)
        assertEquals(pending.cancellationRequestId, retry.cancellationRequestId)
        assertTrue(pending.cancellationRequestId != null)
        assertEquals(1, repository.events.count { it == "CancellationRequested" })
        assertTrue(capacity.releaseAttempts.isEmpty())

        val approved = service.approveCancellationFromMerchant(
            held.id,
            pending.cancellationRequestId!!,
            Instant.now()
        ).block()!!

        assertEquals(CommitmentState.CANCELLED, approved.state)
        assertEquals(1, repository.events.count { it == "CommitmentCancelled" })
        assertEquals(listOf("lease-test"), capacity.releaseAttempts)
    }

    @Test
    fun merchantCancellationRejectionRestoresActiveCommitmentWithoutRelease() {
        val capacity = FakeCapacity()
        val repository = FakeRepository()
        val service = service(capacity, repository)
        val held = service.hold(
            HoldCommand(quoteToken(units = 1), Instant.now().plusSeconds(600), "idem-rejected-cancel")
        ).block()!!
        service.authorizePayment(held.id, "auth-rejected-cancel").block()
        service.confirm(held.id).block()

        val pending = service.cancel(held.id).block()!!
        val restored = service.rejectCancellationFromMerchant(
            held.id,
            pending.cancellationRequestId!!,
            "PREPARATION_ALREADY_STARTED"
        ).block()!!

        assertEquals(CommitmentState.CONFIRMED, restored.state)
        assertEquals(null, restored.cancellationRequestId)
        assertEquals(1, repository.events.count { it == "CancellationRejected" })
        assertTrue(capacity.releaseAttempts.isEmpty())
    }

    @Test
    fun cancellationRetryDoesNotDuplicateCancellationEvent() {
        val capacity = FakeCapacity()
        val repository = FakeRepository()
        val service = service(capacity, repository)
        val held = service.hold(
            HoldCommand(quoteToken(units = 1), Instant.now().plusSeconds(600), "idem-cancel")
        ).block()!!

        val first = service.cancel(held.id).block()!!
        val retry = service.cancel(held.id).block()!!

        assertEquals(CommitmentState.CANCELLED, first.state)
        assertEquals(CommitmentState.CANCELLED, retry.state)
        assertEquals(1, repository.events.count { it == "CommitmentCancelled" })
        assertEquals(2, capacity.releaseAttempts.size)
    }

    @Test
    fun pastPickupIsRejectedBeforeCapacityIsTouched() {
        val capacity = FakeCapacity()
        val service = service(capacity)

        assertThrows(IllegalArgumentException::class.java) {
            service.hold(
                HoldCommand(
                    quoteToken(units = 1),
                    Instant.now().minusSeconds(1),
                    "idem-past"
                )
            )
        }
        assertTrue(capacity.acquiredUnits.isEmpty())
    }
}
