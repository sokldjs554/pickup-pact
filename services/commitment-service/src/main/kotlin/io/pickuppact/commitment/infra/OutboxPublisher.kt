package io.pickuppact.commitment.infra

import java.time.Duration
import org.apache.kafka.clients.producer.ProducerRecord
import org.springframework.kafka.core.KafkaTemplate
import org.springframework.r2dbc.core.DatabaseClient
import org.springframework.scheduling.annotation.Scheduled
import org.springframework.stereotype.Component
import reactor.core.publisher.Mono

@Component
class OutboxPublisher(
    private val db: DatabaseClient,
    private val kafka: KafkaTemplate<String, String>,
) {
    data class OutboxRow(val id: String, val aggregateId: String, val payload: String)

    @Scheduled(fixedDelayString = "\${outbox.publish-delay-ms:500}")
    fun publishBatch() {
        db.sql(
            """
            SELECT id::text AS id, aggregate_id::text AS aggregate_id,
                   jsonb_build_object(
                     'event_id', id::text,
                     'aggregate_id', aggregate_id::text,
                     'event_type', event_type,
                     'occurred_at', occurred_at,
                     'schema_version', 1,
                     'payload', payload
                   )::text AS envelope
            FROM outbox_events
            WHERE published_at IS NULL
            ORDER BY occurred_at, id
            LIMIT 100
            FOR UPDATE SKIP LOCKED
            """.trimIndent(),
        )
            .map { row, _ ->
                OutboxRow(
                    id = row.get("id", String::class.java)!!,
                    aggregateId = row.get("aggregate_id", String::class.java)!!,
                    payload = row.get("envelope", String::class.java)!!,
                )
            }
            .all()
            .concatMap { row ->
                val record = ProducerRecord("pickup.commitment.events.v1", row.aggregateId, row.payload)
                Mono.fromFuture(kafka.send(record))
                    .timeout(Duration.ofSeconds(5))
                    .then(
                        db.sql("UPDATE outbox_events SET published_at = now() WHERE id = cast(:id as uuid) AND published_at IS NULL")
                            .bind("id", row.id)
                            .fetch()
                            .rowsUpdated()
                            .then(),
                    )
            }
            .doOnError { error -> System.err.println("outbox publish failed: ${error.message}") }
            .onErrorResume { Mono.empty() }
            .subscribe()
    }
}
