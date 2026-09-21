package io.pickuppact.commitment.infra

import io.pickuppact.commitment.application.DomainEventPublisher
import org.springframework.context.annotation.Profile
import org.springframework.kafka.core.KafkaTemplate
import org.springframework.stereotype.Component
import reactor.core.publisher.Mono
import java.time.Instant
import java.util.UUID

@Component
@Profile("kafka")
class KafkaDomainEventPublisher(
    private val kafka: KafkaTemplate<String, Any>
) : DomainEventPublisher {
    override fun publish(eventType: String, aggregateId: UUID, payload: Map<String, Any>): Mono<Void> {
        val envelope = mapOf(
            "event_id" to UUID.randomUUID().toString(),
            "aggregate_id" to aggregateId.toString(),
            "type" to eventType,
            "occurred_at" to Instant.now().toString(),
            "payload" to payload
        )
        return Mono.fromFuture(kafka.send("pickup.commitment.events.v1", aggregateId.toString(), envelope))
            .then()
    }
}
