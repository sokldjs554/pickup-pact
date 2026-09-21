package io.pickuppact.ledger.api;

import io.pickuppact.ledger.application.LedgerPostingService;
import io.pickuppact.ledger.domain.*;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.*;

import java.math.BigDecimal;
import java.time.Instant;
import java.util.List;

@RestController
@RequestMapping("/api/v1/ledger")
public class LedgerController {
    private final LedgerPostingService service;

    public LedgerController(LedgerPostingService service) {
        this.service = service;
    }

    public record PostingRequest(String eventId, String aggregateId, String reason, BigDecimal amount) {}

    @PostMapping("/settlement")
    @ResponseStatus(HttpStatus.ACCEPTED)
    public LedgerPostingService.Result settlement(@RequestBody PostingRequest r) {
        String fp = "SETTLEMENT:" + r.aggregateId() + ":" + r.amount();
        var now = Instant.now();
        var batch = new LedgerBatch(
                r.eventId(), fp, r.aggregateId(), r.reason(),
                List.of(
                        new LedgerEntry("platform_payable", LedgerDirection.DEBIT, r.amount(), "KRW", now),
                        new LedgerEntry("merchant_receivable", LedgerDirection.CREDIT, r.amount(), "KRW", now)
                )
        );
        return service.post(batch);
    }
}
