package io.pickuppact.commitment.infra

import com.fasterxml.jackson.databind.ObjectMapper
import io.pickuppact.commitment.application.CommitmentRepository
import io.pickuppact.commitment.application.PendingDomainEvent
import io.pickuppact.commitment.domain.CommitmentState
import io.pickuppact.commitment.domain.PickupCommitment
import io.pickuppact.commitment.domain.PickupPact
import io.pickuppact.commitment.domain.PickupPactStatus
import org.springframework.context.annotation.Profile
import org.springframework.r2dbc.core.DatabaseClient
import org.springframework.stereotype.Repository
import org.springframework.transaction.reactive.TransactionalOperator
import reactor.core.publisher.Flux
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

    private fun upsert(c: PickupCommitment): Mono<PickupCommitment> {
        var spec = db.sql(
            """
            insert into pickup_commitments
              (id, store_id, pickup_at, units, order_amount, lease_token, payment_authorized, state, version,
               idempotency_key, request_fingerprint,
               pact_promised_at, pact_latest_at, pact_compensation_points, pact_version,
               pact_status, pact_compensation_granted, updated_at)
            values (:id, :store, :pickup, :units, :amount, :lease, :payment, :state, :version,
                    :idempotency, :fingerprint,
                    :pactPromised, :pactLatest, :pactPoints, :pactVersion,
                    :pactStatus, :pactGranted, now())
            on conflict (id) do update set
              pickup_at = excluded.pickup_at,
              lease_token = excluded.lease_token,
              state = excluded.state,
              version = excluded.version,
              payment_authorized = excluded.payment_authorized,
              pact_promised_at = excluded.pact_promised_at,
              pact_latest_at = excluded.pact_latest_at,
              pact_compensation_points = excluded.pact_compensation_points,
              pact_version = excluded.pact_version,
              pact_status = excluded.pact_status,
              pact_compensation_granted = excluded.pact_compensation_granted,
              updated_at = now()
            where pickup_commitments.version < excluded.version
            """.trimIndent()
        )
            .bind("id", c.id)
            .bind("store", c.storeId)
            .bind("pickup", OffsetDateTime.ofInstant(c.pickupAt, ZoneOffset.UTC))
            .bind("units", c.units)
            .bind("amount", c.totalAmount)
            .bind("lease", c.leaseToken)
            .bind("payment", c.paymentAuthorized)
            .bind("state", c.state.name)
            .bind("version", c.version)
            .bind("idempotency", c.idempotencyKey)
            .bind("fingerprint", c.requestFingerprint)

        val pact = c.pact
        if (pact == null) {
            spec = spec
                .bindNull("pactPromised", OffsetDateTime::class.java)
                .bindNull("pactLatest", OffsetDateTime::class.java)
                .bindNull("pactPoints", Integer::class.java)
                .bindNull("pactVersion", Integer::class.java)
                .bindNull("pactStatus", String::class.java)
                .bind("pactGranted", false)
        } else {
            spec = spec
                .bind("pactPromised", OffsetDateTime.ofInstant(pact.promisedAt, ZoneOffset.UTC))
                .bind("pactLatest", OffsetDateTime.ofInstant(pact.latestAt, ZoneOffset.UTC))
                .bind("pactPoints", pact.compensationPoints)
                .bind("pactVersion", pact.version)
                .bind("pactStatus", pact.status.name)
                .bind("pactGranted", pact.compensationGranted)
        }

        return spec.fetch()
            .rowsUpdated()
            .flatMap { changed ->
                if (changed == 0L) Mono.error(IllegalStateException("optimistic version conflict"))
                else Mono.just(c)
            }
    }

    override fun save(commitment: PickupCommitment): Mono<PickupCommitment> = upsert(commitment)

    override fun saveWithEvents(
        commitment: PickupCommitment,
        events: List<PendingDomainEvent>
    ): Mono<PickupCommitment> {
        val writeEvents = Flux.fromIterable(events)
            .concatMap { event ->
                val eventId = UUID.randomUUID()
                val json = objectMapper.writeValueAsString(event.payload)
                db.sql(
                    """insert into outbox_events(id, aggregate_id, event_type, payload, occurred_at)
                       values(:id, :aggregate, :type, cast(:payload as jsonb), now())"""
                )
                    .bind("id", eventId)
                    .bind("aggregate", commitment.id)
                    .bind("type", event.eventType)
                    .bind("payload", json)
                    .fetch()
                    .rowsUpdated()
            }
            .then()

        return tx.transactional(
            upsert(commitment).flatMap { saved -> writeEvents.thenReturn(saved) }
        )
    }

    override fun find(id: UUID): Mono<PickupCommitment> =
        queryOne("id = :value", id)
            .switchIfEmpty(Mono.error(NoSuchElementException("commitment not found: $id")))

    override fun findByIdempotencyKey(idempotencyKey: String): Mono<PickupCommitment> =
        queryOne("idempotency_key = :value", idempotencyKey)

    private fun queryOne(predicate: String, value: Any): Mono<PickupCommitment> =
        db.sql(
            """select id, store_id, pickup_at, units, order_amount, lease_token, payment_authorized, state, version,
                      idempotency_key, request_fingerprint,
                      pact_promised_at, pact_latest_at, pact_compensation_points, pact_version,
                      pact_status, pact_compensation_granted
               from pickup_commitments where $predicate"""
        )
            .bind("value", value)
            .map { row, _ ->
                val pactStatus = row.get("pact_status", String::class.java)
                val pact = if (pactStatus == null) null else PickupPact(
                    promisedAt = row.get("pact_promised_at", OffsetDateTime::class.java)!!.toInstant(),
                    latestAt = row.get("pact_latest_at", OffsetDateTime::class.java)!!.toInstant(),
                    compensationPoints = row.get("pact_compensation_points", Integer::class.java)!!.toInt(),
                    version = row.get("pact_version", Integer::class.java)!!.toInt(),
                    status = PickupPactStatus.valueOf(pactStatus),
                    compensationGranted = row.get("pact_compensation_granted", java.lang.Boolean::class.java)?.booleanValue() ?: false
                )
                PickupCommitment(
                    id = row.get("id", UUID::class.java)!!,
                    storeId = row.get("store_id", String::class.java)!!,
                    pickupAt = row.get("pickup_at", OffsetDateTime::class.java)!!.toInstant(),
                    units = row.get("units", Integer::class.java)!!.toInt(),
                    leaseToken = row.get("lease_token", String::class.java)!!,
                    totalAmount = row.get("order_amount", Integer::class.java)!!.toInt(),
                    paymentAuthorized = row.get("payment_authorized", java.lang.Boolean::class.java)!!.booleanValue(),
                    state = CommitmentState.valueOf(row.get("state", String::class.java)!!),
                    version = row.get("version", java.lang.Long::class.java)!!.toLong(),
                    idempotencyKey = row.get("idempotency_key", String::class.java)!!,
                    requestFingerprint = row.get("request_fingerprint", String::class.java)!!,
                    pact = pact
                )
            }
            .one()
            .switchIfEmpty(Mono.empty())
}
