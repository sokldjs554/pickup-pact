package io.pickuppact.commitment.infra

import com.fasterxml.jackson.databind.ObjectMapper
import org.springframework.context.annotation.Profile
import org.springframework.kafka.core.KafkaTemplate
import org.springframework.r2dbc.core.DatabaseClient
import org.springframework.scheduling.annotation.Scheduled
import org.springframework.stereotype.Component
import reactor.core.publisher.Mono
import java.time.OffsetDateTime
import java.util.UUID

@Component
@Profile("postgres & kafka")
class OutboxRelay(
    private val db: DatabaseClient,
    private val kafka: KafkaTemplate<String, String>,
    private val objectMapper: ObjectMapper
) {
    @Scheduled(fixedDelayString = "\${pickup.outbox.poll-ms:500}")
    fun relay(): Mono<Void> =
        db.sql(
            """select id, event_sequence, aggregate_id, event_type, payload::text as payload, occurred_at
               from outbox_events where published_at is null
               order by event_sequence limit 100"""
        )
            .map { row, _ ->
                OutboxRow(
                    row.get("id", UUID::class.java)!!,
                    row.get("event_sequence", java.lang.Long::class.java)!!.toLong(),
                    row.get("aggregate_id", UUID::class.java)!!,
                    row.get("event_type", String::class.java)!!,
                    row.get("payload", String::class.java)!!,
                    row.get("occurred_at", OffsetDateTime::class.java)!!
                )
            }
            .all()
            .concatMap { row ->
                publishCommitment(row)
                    .then(publishDerivedFinancialEffect(row))
                    .then(markPublished(row.id))
            }
            .then()

    private fun publishCommitment(row: OutboxRow): Mono<Void> {
        val envelope = objectMapper.writeValueAsString(
            mapOf(
                "event_id" to row.id.toString(),
                "aggregate_id" to row.aggregateId.toString(),
                "event_type" to row.eventType,
                "occurred_at" to row.occurredAt.toInstant().toString(),
                "schema_version" to 1,
                "payload" to objectMapper.readTree(row.payload)
            )
        )
        return Mono.fromFuture(
            kafka.send("pickup.commitment.events.v1", row.aggregateId.toString(), envelope)
        ).then()
    }

    /**
     * A Pact breach has one deterministic financial side effect: grant the
     * configured compensation points. Using the outbox row id keeps the ledger
     * event id stable across relay retries.
     */
    private fun publishDerivedFinancialEffect(row: OutboxRow): Mono<Void> {
        val payload = objectMapper.readTree(row.payload)
        val posting = when (row.eventType) {
            "PickupPactBreached" -> {
                val compensation = payload.path("compensation_points").asInt(0)
                if (compensation <= 0) {
                    return Mono.error(
                        IllegalStateException("PickupPactBreached requires positive compensation_points")
                    )
                }
                mapOf(
                    "eventId" to "${row.id}-pact-reward",
                    "aggregateId" to row.aggregateId.toString(),
                    "type" to "REWARD",
                    "amount" to compensation
                )
            }
            "PickupClaimed" -> {
                val amount = payload.path("amount").asInt(0)
                if (amount <= 0) return Mono.empty()
                mapOf(
                    "eventId" to "${row.id}-settlement",
                    "aggregateId" to row.aggregateId.toString(),
                    "type" to "SETTLEMENT",
                    "amount" to amount
                )
            }
            else -> return Mono.empty()
        }

        val ledgerPosting = objectMapper.writeValueAsString(posting)
        return Mono.fromFuture(
            kafka.send("pickup.financial.events.v1", row.aggregateId.toString(), ledgerPosting)
        ).then()
    }

    private fun markPublished(id: UUID): Mono<Void> =
        db.sql("update outbox_events set published_at = now() where id = :id and published_at is null")
            .bind("id", id)
            .fetch()
            .rowsUpdated()
            .then()

    private data class OutboxRow(
        val id: UUID,
        val eventSequence: Long,
        val aggregateId: UUID,
        val eventType: String,
        val payload: String,
        val occurredAt: OffsetDateTime
    )
}
