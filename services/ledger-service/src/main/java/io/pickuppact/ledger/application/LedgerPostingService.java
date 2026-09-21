package io.pickuppact.ledger.application;

import io.pickuppact.ledger.domain.LedgerBatch;
import io.pickuppact.ledger.domain.LedgerPostingPolicy;
import io.pickuppact.ledger.domain.LedgerPostingType;
import io.pickuppact.ledger.infra.LedgerRepository;
import java.math.BigDecimal;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class LedgerPostingService {
    public enum Result { POSTED, DUPLICATE_NOOP, CONFLICTING_EVENT_ID }

    private final LedgerRepository repository;

    public LedgerPostingService(LedgerRepository repository) {
        this.repository = repository;
    }

    @Transactional
    public Result post(LedgerPostingType type, String eventId, String aggregateId, BigDecimal amount) {
        return post(LedgerPostingPolicy.posting(type, eventId, aggregateId, amount));
    }

    @Transactional
    public Result post(LedgerBatch batch) {
        if (repository.appendIfAbsent(batch)) {
            return Result.POSTED;
        }

        var current = repository.fingerprint(batch.eventId())
                .orElseThrow(() -> new IllegalStateException("event ID exists without ledger batch fingerprint"));
        return current.equals(batch.semanticFingerprint())
                ? Result.DUPLICATE_NOOP
                : Result.CONFLICTING_EVENT_ID;
    }
}
