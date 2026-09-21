package io.pickuppact.commitment.infra

import org.springframework.context.annotation.Profile
import org.springframework.kafka.core.KafkaTemplate
import org.springframework.r2dbc.core.DatabaseClient
import org.springframework.scheduling.annotation.Scheduled
import org.springframework.stereotype.Component
import reactor.core.publisher.Mono
import java.time.OffsetDateTime
import java.util.UUID

@Component
@Profile("postgres", "kafka")
class OutboxRelay(
    private val db: DatabaseClient,
    private val kafka: KafkaTemplate<String, String>
) {
    @Scheduled(fixedDelayString = "\${pickup.outbox.poll-ms:500}")
    fun relay(): Mono<Void> =
        db.sql(
            """select id, aggregate_id, event_type, payload::text as payload, occurred_at
               from outbox_events where published_at is null
               order by occurred_at, id limit 100"""
        )
            .map { row, _ ->
                OutboxRow(
                    row.get("id", UUID::class.java)!!,
                    row.get("aggregate_id", UUID::class.java)!!,
                    row.get("event_type", String::class.java)!!,
                    row.get("payload", String::class.java)!!,
                    row.get("occurred_at", OffsetDateTime::class.java)!!
                )
            }.all()
            .concatMap { row ->
                val envelope = "{"event_id":"" + row.id +
                    "","aggregate_id":"" + row.aggregateId +
                    "","event_type":"" + row.eventType +
                    "","occurred_at":"" + row.occurredAt.toInstant() +
                    "","schema_version":1,"payload":" + row.payload + "}"
                Mono.fromFuture(kafka.send("pickup.commitment.events.v1", row.aggregateId.toString(), envelope))
                    .then(
                        db.sql("update outbox_events set published_at = now() where id = :id and published_at is null")
                            .bind("id", row.id).fetch().rowsUpdated().then()
                    )
            }.then()

    private data class OutboxRow(
        val id: UUID,
        val aggregateId: UUID,
        val eventType: String,
        val payload: String,
        val occurredAt: OffsetDateTime
    )
}
