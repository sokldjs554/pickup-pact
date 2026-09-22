package io.pickuppact.commitment.domain

import java.time.Duration
import java.time.Instant

enum class PickupPactStatus { ACTIVE, COMPENSATED, FULFILLED, CANCELLED }

data class PickupPact(
    val promisedAt: Instant,
    val latestAt: Instant,
    val compensationPoints: Int,
    val version: Int = 1,
    val status: PickupPactStatus = PickupPactStatus.ACTIVE,
    val compensationGranted: Boolean = false
) {
    init {
        require(latestAt.isAfter(promisedAt)) { "latestAt must be after promisedAt" }
        require(compensationPoints > 0) { "compensationPoints must be positive" }
        require(version > 0) { "version must be positive" }
    }

    fun renegotiate(newPromisedAt: Instant): PickupPact {
        check(status == PickupPactStatus.ACTIVE) { "only an active pact can be renegotiated" }
        return copy(
            promisedAt = newPromisedAt,
            latestAt = newPromisedAt.plus(PickupPactPolicy.guaranteeWindow),
            version = version + 1,
            compensationGranted = false
        )
    }

    fun breach(): PickupPact {
        check(status == PickupPactStatus.ACTIVE) { "only an active pact can be breached" }
        check(!compensationGranted) { "compensation already granted" }
        return copy(
            status = PickupPactStatus.COMPENSATED,
            compensationGranted = true
        )
    }

    fun fulfill(): PickupPact {
        check(status in setOf(PickupPactStatus.ACTIVE, PickupPactStatus.COMPENSATED)) {
            "only an active or compensated pact can be fulfilled"
        }
        return if (status == PickupPactStatus.COMPENSATED) this
        else copy(status = PickupPactStatus.FULFILLED)
    }

    fun cancel(): PickupPact {
        check(status != PickupPactStatus.FULFILLED) { "fulfilled pact cannot be cancelled" }
        return copy(status = PickupPactStatus.CANCELLED)
    }
}

object PickupPactPolicy {
    val guaranteeWindow: Duration = Duration.ofMinutes(3)
    const val compensationPoints: Int = 500

    fun issue(pickupAt: Instant): PickupPact =
        PickupPact(
            promisedAt = pickupAt,
            latestAt = pickupAt.plus(guaranteeWindow),
            compensationPoints = compensationPoints
        )
}
