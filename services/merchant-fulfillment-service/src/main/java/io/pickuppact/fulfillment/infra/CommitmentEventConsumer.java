package io.pickuppact.fulfillment.infra;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.pickuppact.fulfillment.application.MerchantFulfillmentService;
import java.time.Instant;
import java.util.UUID;
import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.stereotype.Component;

@Component
public class CommitmentEventConsumer {
    private final MerchantFulfillmentService service;
    private final ObjectMapper objectMapper;

    public CommitmentEventConsumer(MerchantFulfillmentService service, ObjectMapper objectMapper) {
        this.service = service;
        this.objectMapper = objectMapper;
    }

    @KafkaListener(topics = "pickup.commitment.events.v1", groupId = "merchant-fulfillment")
    public void consume(String raw) {
        try {
            JsonNode envelope = objectMapper.readTree(raw);
            service.handleCommitmentEvent(
                    envelope.path("event_id").asText(),
                    UUID.fromString(envelope.path("aggregate_id").asText()),
                    envelope.path("event_type").asText(),
                    Instant.parse(envelope.path("occurred_at").asText()),
                    envelope.path("payload")
            );
        } catch (Exception exception) {
            throw new IllegalArgumentException("invalid commitment event for merchant fulfillment", exception);
        }
    }
}
