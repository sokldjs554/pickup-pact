package io.pickuppact.ledger.infra;

import io.pickuppact.ledger.api.LedgerPostingRequest;
import io.pickuppact.ledger.application.LedgerPostingService;
import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.stereotype.Component;

@Component
public class LedgerEventConsumer {
    private final LedgerPostingService service;

    public LedgerEventConsumer(LedgerPostingService service) {
        this.service = service;
    }

    @KafkaListener(topics = "pickup.financial.events.v1", groupId = "pickup-ledger")
    public void consume(LedgerPostingRequest request) {
        var result = service.post(
                request.type(),
                request.eventId(),
                request.aggregateId(),
                request.amount()
        );
        if (result == LedgerPostingService.Result.CONFLICTING_EVENT_ID) {
            throw new IllegalStateException("conflicting event ID must not be acknowledged as a safe redelivery");
        }
    }
}
