package io.pickuppact.ledger.infra;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.pickuppact.ledger.api.LedgerPostingRequest;
import io.pickuppact.ledger.application.LedgerPostingService;
import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.stereotype.Component;

@Component
public class LedgerEventConsumer {
    private final LedgerPostingService service;
    private final ObjectMapper objectMapper;

    public LedgerEventConsumer(LedgerPostingService service, ObjectMapper objectMapper) {
        this.service = service;
        this.objectMapper = objectMapper;
    }

    @KafkaListener(topics = "pickup.financial.events.v1", groupId = "pickup-ledger")
    public void consume(String payload) {
        final LedgerPostingRequest request;
        try {
            request = objectMapper.readValue(payload, LedgerPostingRequest.class);
        } catch (JsonProcessingException exception) {
            throw new IllegalArgumentException("invalid financial event JSON", exception);
        }

        var result = service.post(
                request.type(),
                request.eventId(),
                request.aggregateId(),
                request.amount(),
                request.sourceEventId()
        );
        if (result == LedgerPostingService.Result.CONFLICTING_EVENT_ID
                || result == LedgerPostingService.Result.SOURCE_POSTING_CONFLICT) {
            System.err.printf(
                    "quarantined conflicting financial event: eventId=%s aggregateId=%s%n",
                    request.eventId(),
                    request.aggregateId()
            );
        }
    }
}
