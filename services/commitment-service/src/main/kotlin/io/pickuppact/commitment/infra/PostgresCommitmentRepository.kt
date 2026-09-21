package io.pickuppact.commitment.infra

import com.fasterxml.jackson.databind.ObjectMapper
import io.pickuppact.commitment.application.CommitmentRepository
import io.pickuppact.commitment.domain.CommitmentState
import io.pickuppact.commitment.domain.PickupCommitment
import org.springframework.context.annotation.Profile
import org.springframework.r2dbc.core.DatabaseClient
import org.springframework.stereotype.Repository
import org.springframework.transaction.reactive.TransactionalOperator
import reactor.core.publisher.Mono
import java.time.OffsetDateTime
import java.time.ZoneOffset
import java.util.UUID

@Repository
@Profile("postgres")
class PostgresCommitmentRepository(
    private val db: DatabaseClient,
    private val tx: TransactionalOperator,
    private val objectMapper: ObjectMapper
) : CommitmentRepository {

    private fun upsert(c: PickupCommitment): Mono<PickupCommitment> =
        db.sql(
            """
            insert into pickup_commitments
              (id, store_id, pickup_at, units, lease_token, payment_authorized, state, version, updated_at)
            values (:id, :store, :pickup, :units, :lease, :payment, :state, :version, now())
            on conflict (id) do update set
              state = excluded.state,
              version = excluded.version,
              payment_authorized = excluded.payment_authorized,
              updated_at = now()
            where pickup_commitments.version < excluded.version
            """.trimIndent()
        )
            .bind("id", c.id)
            .bind("store", c.storeId)
            .bind("pickup", OffsetDateTime.ofInstant(c.pickupAt, ZoneOffset.UTC))
            .bind("units", c.units)
            .bind("lease", c.leaseToken)
            .bind("payment", c.paymentAuthorized)
            .bind("state", c.state.name)
            .bind("version", c.version)
            .fetch()
            .rowsUpdated()
            .flatMap { changed ->
                if (changed == 0L) Mono.error(IllegalStateException("optimistic version conflict"))
                else Mono.just(c)
            }

    override fun save(commitment: PickupCommitment): Mono<PickupCommitment> = upsert(commitment)

    override fun saveWithEvent(
        commitment: PickupCommitment,
        eventType: String,
        payload: Map<String, Any>
    ): Mono<PickupCommitment> {
        val eventId = UUID.randomUUID()
        val json = objectMapper.writeValueAsString(payload)
        val writeEvent = db.sql(
            """insert into outbox_events(id, aggregate_id, event_type, payload, occurred_at)
               values(:id, :aggregate, :type, cast(:payload as jsonb), now())"""
        )
            .bind("id", eventId)
            .bind("aggregate", commitment.id)
            .bind("type", eventType)
            .bind("payload", json)
            .fetch()
            .rowsUpdated()
            .then()

        return tx.transactional(
            upsert(commitment).flatMap { saved -> writeEvent.thenReturn(saved) }
        )
    }

    override fun find(id: UUID): Mono<PickupCommitment> =
        db.sql(
            """select id, store_id, pickup_at, units, lease_token, payment_authorized, state, version
               from pickup_commitments where id = :id"""
        )
            .bind("id", id)
            .map { row, _ ->
                PickupCommitment(
                    id = row.get("id", UUID::class.java)!!,
                    storeId = row.get("store_id", String::class.java)!!,
                    pickupAt = row.get("pickup_at", OffsetDateTime::class.java)!!.toInstant(),
                    units = row.get("units", Integer::class.java)!!.toInt(),
                    leaseToken = row.get("lease_token", String::class.java)!!,
                    paymentAuthorized = row.get("payment_authorized", java.lang.Boolean::class.java)!!.booleanValue(),
                    state = CommitmentState.valueOf(row.get("state", String::class.java)!!),
                    version = row.get("version", java.lang.Long::class.java)!!.toLong()
                )
            }
            .one()
            .switchIfEmpty(Mono.error(NoSuchElementException("commitment not found: $id")))
}
