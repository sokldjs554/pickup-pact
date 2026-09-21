package io.pickuppact.ledger.api;

import io.pickuppact.ledger.application.LedgerPostingService;
import io.pickuppact.ledger.domain.LedgerBatchSummary;
import io.pickuppact.ledger.domain.LedgerConflict;
import jakarta.validation.Valid;
import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import java.util.List;
import java.util.Map;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.validation.annotation.Validated;
import org.springframework.web.bind.annotation.*;

@RestController
@Validated
@RequestMapping("/api/v1/ledger")
public class LedgerController {
    private final LedgerPostingService service;

    public LedgerController(LedgerPostingService service) {
        this.service = service;
    }

    @PostMapping("/postings")
    public ResponseEntity<Map<String, String>> posting(@Valid @RequestBody LedgerPostingRequest request) {
        var result = service.post(request.type(), request.eventId(), request.aggregateId(), request.amount());
        var body = Map.of("result", result.name());
        return switch (result) {
            case POSTED -> ResponseEntity.status(HttpStatus.ACCEPTED).body(body);
            case DUPLICATE_NOOP -> ResponseEntity.ok(body);
            case CONFLICTING_EVENT_ID -> ResponseEntity.status(HttpStatus.CONFLICT).body(body);
        };
    }

    @GetMapping("/orders/{aggregateId}")
    public List<LedgerBatchSummary> history(
            @PathVariable String aggregateId,
            @RequestParam(defaultValue = "50") @Min(1) @Max(100) int limit
    ) {
        return service.history(aggregateId, limit);
    }

    @GetMapping("/conflicts")
    public List<LedgerConflict> conflicts(
            @RequestParam(defaultValue = "50") @Min(1) @Max(100) int limit
    ) {
        return service.conflicts(limit);
    }
}
