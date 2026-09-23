package io.pickuppact.commitment.infra

import com.fasterxml.jackson.databind.ObjectMapper
import io.pickuppact.commitment.application.CommitmentService
import org.springframework.kafka.annotation.KafkaListener
import org.springframework.stereotype.Component
import java.time.Instant
import java.util.UUID

@Component
class FulfillmentEventConsumer(
    private val service: CommitmentService,
    private val objectMapper: ObjectMapper,
) {
    @KafkaListener(
        topics = ["pickup.fulfillment.events.v1"],
        groupId = "pickup-commitment-fulfillment",
    )
    fun consume(raw: String) {
        val envelope = objectMapper.readTree(raw)
        val eventType = envelope.path("event_type").asText()
        val payload = envelope.path("payload")
        val orderId = UUID.fromString(envelope.path("aggregate_id").asText())

        when (eventType) {
            "FulfillmentAnomalyDetected" -> {
                if (payload.path("code").asText() != "READY_LATE") return
                val observedAt = Instant.parse(payload.path("observed_at").asText())
                service.breachPactFromFulfillment(orderId, observedAt).block()
            }

            "MerchantCancellationApproved" -> {
                val requestId = UUID.fromString(payload.path("request_id").asText())
                val decidedAt = Instant.parse(payload.path("decided_at").asText())
                service.approveCancellationFromMerchant(orderId, requestId, decidedAt).block()
            }

            "MerchantCancellationRejected" -> {
                val requestId = UUID.fromString(payload.path("request_id").asText())
                val reason = payload.path("reason").asText("merchant_rejected")
                service.rejectCancellationFromMerchant(orderId, requestId, reason).block()
            }
        }
    }
}
