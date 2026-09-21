package io.pickuppact.ledger.api;

import io.pickuppact.ledger.application.LedgerPostingService;
import jakarta.validation.Valid;
import java.util.Map;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/v1/ledger")
public class LedgerController {
    private final LedgerPostingService service;

    public LedgerController(LedgerPostingService service) {
        this.service = service;
    }

    @PostMapping("/postings")
    public ResponseEntity<Map<String, String>> posting(@Valid @RequestBody LedgerPostingRequest request) {
        var result = service.post(
                request.type(),
                request.eventId(),
                request.aggregateId(),
                request.amount()
        );

        var body = Map.of("result", result.name());
        return switch (result) {
            case POSTED -> ResponseEntity.status(HttpStatus.ACCEPTED).body(body);
            case DUPLICATE_NOOP -> ResponseEntity.ok(body);
            case CONFLICTING_EVENT_ID -> ResponseEntity.status(HttpStatus.CONFLICT).body(body);
        };
    }
}
