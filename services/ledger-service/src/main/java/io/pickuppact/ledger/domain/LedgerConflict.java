package io.pickuppact.ledger.domain;

import java.time.Instant;

public record LedgerConflict(
        String eventId,
        String aggregateId,
        String reason,
        String existingFingerprint,
        String incomingFingerprint,
        Instant observedAt
) {}
