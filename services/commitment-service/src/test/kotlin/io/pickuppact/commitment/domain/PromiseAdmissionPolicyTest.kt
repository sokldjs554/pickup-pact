package io.pickuppact.commitment.domain

import com.fasterxml.jackson.module.kotlin.jacksonObjectMapper
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test

class PromiseAdmissionPolicyTest {
    private val mapper = jacksonObjectMapper()

    @Test
    fun goldenCasesStayAlignedWithThePublicDemoPolicy() {
        val stream = requireNotNull(javaClass.classLoader.getResourceAsStream("promise-admission-cases.json"))
        val cases = mapper.readTree(stream)

        cases.forEach { item ->
            val input = item["input"]
            val expected = item["expected"]
            val quote = PromiseAdmissionPolicy.quote(
                PromiseAdmissionInput(
                    backlogUnits = input["backlog_units"].asInt(),
                    orderUnits = input["order_units"].asInt(),
                    serviceRateUnitsPerMinute = input["service_rate_units_per_minute"].asDouble(),
                    travelMinutes = input["travel_minutes"].asInt(),
                    maxPromiseMinutes = input["max_promise_minutes"].asInt(),
                    safetyMinutes = input["safety_minutes"].asInt()
                )
            )

            assertEquals(expected["decision"].asText(), quote.decision.name, item["name"].asText())
            assertEquals(expected["earliest_ready_minutes"].asInt(), quote.earliestReadyMinutes)
            if (expected["quoted_minutes"].isNull) {
                assertEquals(null, quote.quotedMinutes)
            } else {
                assertEquals(expected["quoted_minutes"].asInt(), quote.quotedMinutes)
            }
            assertEquals(expected["retry_after_minutes"].asInt(), quote.retryAfterMinutes)
            assertEquals(expected["queue_cap_units"].asInt(), quote.queueCapUnits)
            assertEquals(expected["reason"].asText(), quote.reason)
        }
    }
}
