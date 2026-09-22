package io.pickuppact.fulfillment.infra;

import com.fasterxml.jackson.databind.ObjectMapper;
import java.sql.Timestamp;
import java.util.Map;
import java.util.UUID;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;

@Component
public class FulfillmentOutboxRelay {
    private final JdbcTemplate jdbc;
    private final KafkaTemplate<String, String> kafka;
    private final ObjectMapper objectMapper;

    public FulfillmentOutboxRelay(
            JdbcTemplate jdbc,
            KafkaTemplate<String, String> kafka,
            ObjectMapper objectMapper
    ) {
        this.jdbc = jdbc;
        this.kafka = kafka;
        this.objectMapper = objectMapper;
    }

    @Scheduled(fixedDelayString = "${fulfillment.outbox.poll-ms:500}")
    public void relay() {
        var rows = jdbc.query(
                """
                select id, event_sequence, aggregate_id, event_type, payload::text, occurred_at
                from fulfillment_outbox_events
                where published_at is null
                order by event_sequence
                limit 100
                """,
                (rs, rowNum) -> new OutboxRow(
                        rs.getObject("id", UUID.class),
                        rs.getLong("event_sequence"),
                        rs.getObject("aggregate_id", UUID.class),
                        rs.getString("event_type"),
                        rs.getString("payload"),
                        rs.getTimestamp("occurred_at")
                )
        );
        for (var row : rows) publish(row);
    }

    @Transactional
    protected void publish(OutboxRow row) {
        try {
            String envelope = objectMapper.writeValueAsString(Map.of(
                    "event_id", row.id().toString(),
                    "aggregate_id", row.aggregateId().toString(),
                    "event_type", row.eventType(),
                    "occurred_at", row.occurredAt().toInstant().toString(),
                    "schema_version", 1,
                    "payload", objectMapper.readTree(row.payload())
            ));
            kafka.send("pickup.fulfillment.events.v1", row.aggregateId().toString(), envelope).get();
            jdbc.update(
                    "update fulfillment_outbox_events set published_at=now() where id=? and published_at is null",
                    row.id()
            );
        } catch (Exception exception) {
            throw new IllegalStateException("failed to publish fulfillment outbox row " + row.sequence(), exception);
        }
    }

    private record OutboxRow(
            UUID id,
            long sequence,
            UUID aggregateId,
            String eventType,
            String payload,
            Timestamp occurredAt
    ) {}
}
