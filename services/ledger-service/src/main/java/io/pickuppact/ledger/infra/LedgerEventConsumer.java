package io.pickuppact.ledger.infra;

import io.pickuppact.ledger.api.LedgerController.PostingRequest;
import io.pickuppact.ledger.api.LedgerController;
import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.stereotype.Component;

@Component
public class LedgerEventConsumer {
    private final LedgerController controller;

    public LedgerEventConsumer(LedgerController controller) {
        this.controller = controller;
    }

    @KafkaListener(topics = "pickup.financial.events.v1", groupId = "pickup-ledger")
    public void consume(PostingRequest request) {
        controller.settlement(request);
    }
}
