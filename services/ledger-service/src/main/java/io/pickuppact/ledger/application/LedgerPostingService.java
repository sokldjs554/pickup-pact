package io.pickuppact.ledger.application;

import io.pickuppact.ledger.domain.LedgerBatch;
import io.pickuppact.ledger.infra.LedgerRepository;
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
    public Result post(LedgerBatch batch) {
        var current = repository.fingerprint(batch.eventId());
        if (current.isPresent()) {
            return current.get().equals(batch.semanticFingerprint())
                    ? Result.DUPLICATE_NOOP
                    : Result.CONFLICTING_EVENT_ID;
        }
        repository.append(batch);
        return Result.POSTED;
    }
}
