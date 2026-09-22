package io.pickuppact.fulfillment.infra;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.pickuppact.fulfillment.domain.FulfillmentAnomalyCode;
import io.pickuppact.fulfillment.domain.FulfillmentState;
import io.pickuppact.fulfillment.domain.JitPreparationPolicy;
import io.pickuppact.fulfillment.domain.MerchantOrder;
import java.sql.Timestamp;
import java.time.Instant;
import java.util.List;
import java.util.Optional;
import java.util.UUID;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

@Repository
public class MerchantFulfillmentRepository {
    private final JdbcTemplate jdbc;
    private final ObjectMapper objectMapper;

    public MerchantFulfillmentRepository(JdbcTemplate jdbc, ObjectMapper objectMapper) {
        this.jdbc = jdbc;
        this.objectMapper = objectMapper;
    }

    public boolean recordInbox(String eventId, UUID aggregateId, String eventType, Object payload) {
        return jdbc.update(
                """
                insert into merchant_inbox_events(event_id, aggregate_id, event_type, payload)
                values(?,?,?,?::jsonb)
                on conflict (event_id) do nothing
                """,
                eventId, aggregateId, eventType, json(payload)
        ) == 1;
    }

    public void insert(MerchantOrder order) {
        jdbc.update(
                """
                insert into merchant_orders(
                  order_id, store_id, pickup_at, capacity_units, state,
                  preparation_seconds, earliest_start_at, target_ready_at, latest_ready_at,
                  accepted_at, started_at, ready_at, picked_up_at, cancellation_requested_at,
                  version, updated_at
                )
                values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,now())
                on conflict (order_id) do nothing
                """,
                order.orderId(), order.storeId(), Timestamp.from(order.pickupAt()),
                order.capacityUnits(), order.state().name(), order.window().preparationSeconds(),
                Timestamp.from(order.window().earliestStartAt()),
                Timestamp.from(order.window().targetReadyAt()),
                Timestamp.from(order.window().latestReadyAt()),
                ts(order.acceptedAt()), ts(order.startedAt()), ts(order.readyAt()),
                ts(order.pickedUpAt()), ts(order.cancellationRequestedAt()), order.version()
        );
    }

    public void update(MerchantOrder order) {
        int changed = jdbc.update(
                """
                update merchant_orders set
                  pickup_at=?, state=?,
                  preparation_seconds=?, earliest_start_at=?, target_ready_at=?, latest_ready_at=?,
                  accepted_at=?, started_at=?, ready_at=?, picked_up_at=?,
                  cancellation_requested_at=?, version=?, updated_at=now()
                where order_id=? and version=?
                """,
                Timestamp.from(order.pickupAt()), order.state().name(),
                order.window().preparationSeconds(),
                Timestamp.from(order.window().earliestStartAt()),
                Timestamp.from(order.window().targetReadyAt()),
                Timestamp.from(order.window().latestReadyAt()),
                ts(order.acceptedAt()), ts(order.startedAt()), ts(order.readyAt()),
                ts(order.pickedUpAt()), ts(order.cancellationRequestedAt()),
                order.version(), order.orderId(), order.version() - 1
        );
        if (changed != 1) throw new IllegalStateException("merchant order optimistic version conflict");
    }

    public Optional<MerchantOrder> find(UUID orderId) {
        return jdbc.query(
                """
                select order_id, store_id, pickup_at, capacity_units, state,
                       preparation_seconds, earliest_start_at, target_ready_at, latest_ready_at,
                       accepted_at, started_at, ready_at, picked_up_at, cancellation_requested_at, version
                from merchant_orders where order_id=?
                """,
                (rs, rowNum) -> new MerchantOrder(
                        rs.getObject("order_id", UUID.class),
                        rs.getString("store_id"),
                        rs.getTimestamp("pickup_at").toInstant(),
                        rs.getInt("capacity_units"),
                        FulfillmentState.valueOf(rs.getString("state")),
                        new JitPreparationPolicy.Window(
                                rs.getInt("preparation_seconds"),
                                rs.getTimestamp("earliest_start_at").toInstant(),
                                rs.getTimestamp("target_ready_at").toInstant(),
                                rs.getTimestamp("latest_ready_at").toInstant()
                        ),
                        instant(rs.getTimestamp("accepted_at")),
                        instant(rs.getTimestamp("started_at")),
                        instant(rs.getTimestamp("ready_at")),
                        instant(rs.getTimestamp("picked_up_at")),
                        instant(rs.getTimestamp("cancellation_requested_at")),
                        rs.getLong("version")
                ),
                orderId
        ).stream().findFirst();
    }

    public void enqueueDelivery(UUID orderId, String storeId, String deliveryType, Object payload) {
        jdbc.update(
                """
                insert into merchant_deliveries(order_id, store_id, delivery_type, payload)
                values(?,?,?,?::jsonb)
                on conflict (order_id, delivery_type) do nothing
                """,
                orderId, storeId, deliveryType, json(payload)
        );
    }

    public List<Delivery> pendingDeliveries(String storeId, long afterSequence, int limit) {
        return jdbc.query(
                """
                select delivery_sequence, order_id, store_id, delivery_type, payload::text,
                       acknowledged_at, created_at
                from merchant_deliveries
                where store_id=? and delivery_sequence>? and acknowledged_at is null
                order by delivery_sequence
                limit ?
                """,
                (rs, rowNum) -> new Delivery(
                        rs.getLong("delivery_sequence"),
                        rs.getObject("order_id", UUID.class),
                        rs.getString("store_id"),
                        rs.getString("delivery_type"),
                        rs.getString("payload"),
                        instant(rs.getTimestamp("acknowledged_at")),
                        rs.getTimestamp("created_at").toInstant()
                ),
                storeId, afterSequence, limit
        );
    }

    public boolean acknowledge(long sequence) {
        return jdbc.update(
                "update merchant_deliveries set acknowledged_at=now() where delivery_sequence=? and acknowledged_at is null",
                sequence
        ) == 1;
    }

    public void recordEffect(UUID orderId, String effectType, Object payload) {
        jdbc.update(
                """
                insert into merchant_effects(order_id, effect_type, payload)
                values(?,?,?::jsonb)
                on conflict (order_id, effect_type) do nothing
                """,
                orderId, effectType, json(payload)
        );
    }

    public List<Effect> effects(UUID orderId) {
        return jdbc.query(
                """
                select effect_type, payload::text, created_at
                from merchant_effects where order_id=? order by id
                """,
                (rs, rowNum) -> new Effect(
                        rs.getString("effect_type"),
                        rs.getString("payload"),
                        rs.getTimestamp("created_at").toInstant()
                ),
                orderId
        );
    }

    public void recordAnomaly(
            UUID orderId,
            FulfillmentAnomalyCode code,
            String detail,
            Instant observedAt
    ) {
        jdbc.update(
                """
                insert into merchant_fulfillment_anomalies(order_id, code, detail, observed_at)
                values(?,?,?,?)
                on conflict (order_id, code, observed_at) do nothing
                """,
                orderId, code.name(), detail, Timestamp.from(observedAt)
        );
    }

    public List<Anomaly> anomalies(UUID orderId) {
        return jdbc.query(
                """
                select code, detail, observed_at
                from merchant_fulfillment_anomalies
                where order_id=? order by observed_at, id
                """,
                (rs, rowNum) -> new Anomaly(
                        FulfillmentAnomalyCode.valueOf(rs.getString("code")),
                        rs.getString("detail"),
                        rs.getTimestamp("observed_at").toInstant()
                ),
                orderId
        );
    }

    public void appendOutbox(UUID aggregateId, String eventType, Object payload, Instant occurredAt) {
        jdbc.update(
                """
                insert into fulfillment_outbox_events(id, aggregate_id, event_type, payload, occurred_at)
                values(?,?,?,?::jsonb,?)
                """,
                UUID.randomUUID(), aggregateId, eventType, json(payload), Timestamp.from(occurredAt)
        );
    }

    private String json(Object value) {
        try {
            return objectMapper.writeValueAsString(value);
        } catch (JsonProcessingException exception) {
            throw new IllegalArgumentException("cannot encode merchant payload", exception);
        }
    }

    private static Timestamp ts(Instant value) {
        return value == null ? null : Timestamp.from(value);
    }

    private static Instant instant(Timestamp value) {
        return value == null ? null : value.toInstant();
    }

    public record Delivery(
            long sequence,
            UUID orderId,
            String storeId,
            String type,
            String payload,
            Instant acknowledgedAt,
            Instant createdAt
    ) {}

    public record Effect(String type, String payload, Instant createdAt) {}
    public record Anomaly(FulfillmentAnomalyCode code, String detail, Instant observedAt) {}
}
