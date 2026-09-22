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
        if (envelope.path("event_type").asText() != "FulfillmentAnomalyDetected") return

        val payload = envelope.path("payload")
        if (payload.path("code").asText() != "READY_LATE") return

        val orderId = UUID.fromString(envelope.path("aggregate_id").asText())
        val observedAt = Instant.parse(payload.path("observed_at").asText())

        service.breachPactFromFulfillment(orderId, observedAt).block()
    }
}
