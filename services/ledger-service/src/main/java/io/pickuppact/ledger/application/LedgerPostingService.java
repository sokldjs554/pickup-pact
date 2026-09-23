package io.pickuppact.ledger.application;

import io.pickuppact.ledger.domain.LedgerBatch;
import io.pickuppact.ledger.domain.LedgerBatchSummary;
import io.pickuppact.ledger.domain.LedgerConflict;
import io.pickuppact.ledger.domain.LedgerPostingPolicy;
import io.pickuppact.ledger.domain.LedgerPostingType;
import io.pickuppact.ledger.infra.LedgerRepository;
import java.math.BigDecimal;
import java.util.List;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class LedgerPostingService {
    public enum Result { POSTED, DUPLICATE_NOOP, CONFLICTING_EVENT_ID, SOURCE_POSTING_CONFLICT }

    private final LedgerRepository repository;

    public LedgerPostingService(LedgerRepository repository) {
        this.repository = repository;
    }

    @Transactional
    public Result post(
            LedgerPostingType type,
            String eventId,
            String aggregateId,
            BigDecimal amount,
            String sourceEventId
    ) {
        return post(LedgerPostingPolicy.posting(type, eventId, aggregateId, amount, sourceEventId));
    }

    @Transactional
    public Result post(LedgerBatch batch) {
        var existing = repository.fingerprint(batch.eventId());
        if (existing.isPresent()) {
            if (existing.get().equals(batch.semanticFingerprint())) {
                return Result.DUPLICATE_NOOP;
            }
            repository.recordConflict(batch, existing.get());
            return Result.CONFLICTING_EVENT_ID;
        }

        if (batch.reversal() && !repository.reversalAllowed(batch)) {
            // Another transaction may have committed this exact event while
            // we were waiting on the source-posting lock. Re-check event
            // identity before reporting a source-balance conflict.
            var concurrent = repository.fingerprint(batch.eventId());
            if (concurrent.isPresent()) {
                if (concurrent.get().equals(batch.semanticFingerprint())) {
                    return Result.DUPLICATE_NOOP;
                }
                repository.recordConflict(batch, concurrent.get());
                return Result.CONFLICTING_EVENT_ID;
            }
            return Result.SOURCE_POSTING_CONFLICT;
        }

        if (repository.appendIfAbsent(batch)) return Result.POSTED;

        var current = repository.fingerprint(batch.eventId())
                .orElseThrow(() -> new IllegalStateException("event ID exists without ledger batch fingerprint"));
        if (current.equals(batch.semanticFingerprint())) return Result.DUPLICATE_NOOP;

        repository.recordConflict(batch, current);
        return Result.CONFLICTING_EVENT_ID;
    }

    @Transactional(readOnly = true)
    public List<LedgerBatchSummary> history(String aggregateId, int limit) {
        return repository.history(aggregateId, limit);
    }

    @Transactional(readOnly = true)
    public List<LedgerConflict> conflicts(int limit) {
        return repository.conflicts(limit);
    }
}
