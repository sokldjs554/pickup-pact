package io.pickuppact.commitment.infra

import java.time.Duration
import org.apache.kafka.clients.producer.ProducerRecord
import org.springframework.kafka.core.KafkaTemplate
import org.springframework.r2dbc.core.DatabaseClient
import org.springframework.scheduling.annotation.Scheduled
import org.springframework.stereotype.Component
import reactor.core.publisher.Flux

@Component
class OutboxPublisher(
    private val db: DatabaseClient,
    private val kafka: KafkaTemplate<String, String>,
) {
    data class OutboxRow(val eventId: String, val aggregateId: String, val payload: String)

    @Scheduled(fixedDelayString = "${'$'}{outbox.publish-delay-ms:500}")
    fun publishBatch() {
        db.sql(
            """
            SELECT event_id, aggregate_id, payload_json::text AS payload
            FROM outbox_event
            WHERE published_at IS NULL
            ORDER BY occurred_at, event_id
            LIMIT 100
            FOR UPDATE SKIP LOCKED
            """.trimIndent(),
        ).map { row, _ ->
            OutboxRow(
                eventId = row.get("event_id", String::class.java)!!,
                aggregateId = row.get("aggregate_id", String::class.java)!!,
                payload = row.get("payload", String::class.java)!!,
            )
        }.all()
            .concatMap { row ->
                val record = ProducerRecord("pickup.commitment.events.v1", row.aggregateId, row.payload)
                reactor.core.publisher.Mono.fromFuture(kafka.send(record))
                    .timeout(Duration.ofSeconds(5))
                    .then(
                        db.sql("UPDATE outbox_event SET published_at = now() WHERE event_id = :id AND published_at IS NULL")
                            .bind("id", row.eventId).fetch().rowsUpdated().then(),
                    )
            }
            .onErrorResume { Flux.empty() }
            .subscribe()
    }
}
