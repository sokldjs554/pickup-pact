package io.pickuppact.commitment.domain

import kotlin.math.ceil
import kotlin.math.floor
import kotlin.math.max

enum class AdmissionDecision { ACCEPT, OFFER_LATER, PAUSE }

data class PromiseAdmissionInput(
    val backlogUnits: Int,
    val orderUnits: Int,
    val serviceRateUnitsPerMinute: Double,
    val travelMinutes: Int,
    val maxPromiseMinutes: Int = 18,
    val safetyMinutes: Int = 2
)

data class PromiseAdmissionQuote(
    val decision: AdmissionDecision,
    val earliestReadyMinutes: Int,
    val quotedMinutes: Int?,
    val retryAfterMinutes: Int,
    val queueCapUnits: Int,
    val reason: String
)

object PromiseAdmissionPolicy {
    fun quote(input: PromiseAdmissionInput): PromiseAdmissionQuote {
        require(input.backlogUnits >= 0) { "backlogUnits must be non-negative" }
        require(input.orderUnits > 0) { "orderUnits must be positive" }
        require(input.serviceRateUnitsPerMinute > 0) { "service rate must be positive" }
        require(input.travelMinutes >= 0) { "travelMinutes must be non-negative" }
        require(input.maxPromiseMinutes > 0) { "maxPromiseMinutes must be positive" }
        require(input.safetyMinutes >= 0) { "safetyMinutes must be non-negative" }

        val totalUnits = input.backlogUnits + input.orderUnits
        val queueCapUnits = max(
            input.orderUnits,
            floor(input.serviceRateUnitsPerMinute * input.maxPromiseMinutes).toInt()
        )
        val workMinutes = ceil(totalUnits / input.serviceRateUnitsPerMinute).toInt()
        val earliestReady = workMinutes + input.safetyMinutes

        if (totalUnits > queueCapUnits || earliestReady > input.maxPromiseMinutes) {
            val excessUnits = max(1, totalUnits - queueCapUnits)
            val retryAfter = max(
                1,
                ceil(excessUnits / input.serviceRateUnitsPerMinute).toInt()
            )
            return PromiseAdmissionQuote(
                decision = AdmissionDecision.PAUSE,
                earliestReadyMinutes = earliestReady,
                quotedMinutes = null,
                retryAfterMinutes = retryAfter,
                queueCapUnits = queueCapUnits,
                reason = "REMOTE_QUEUE_CAP"
            )
        }

        val quoted = max(input.travelMinutes, earliestReady)
        if (earliestReady <= input.travelMinutes + 1) {
            return PromiseAdmissionQuote(
                decision = AdmissionDecision.ACCEPT,
                earliestReadyMinutes = earliestReady,
                quotedMinutes = quoted,
                retryAfterMinutes = 0,
                queueCapUnits = queueCapUnits,
                reason = "ARRIVAL_ALIGNED"
            )
        }

        return PromiseAdmissionQuote(
            decision = AdmissionDecision.OFFER_LATER,
            earliestReadyMinutes = earliestReady,
            quotedMinutes = quoted,
            retryAfterMinutes = 0,
            queueCapUnits = queueCapUnits,
            reason = "LATER_PROMISE_REQUIRED"
        )
    }
}
