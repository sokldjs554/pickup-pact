package io.pickuppact.ledger.api;

import io.pickuppact.ledger.domain.LedgerPostingType;
import jakarta.validation.constraints.DecimalMin;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import java.math.BigDecimal;

public record LedgerPostingRequest(
        @NotBlank String eventId,
        @NotBlank String aggregateId,
        @NotNull LedgerPostingType type,
        @NotNull @DecimalMin(value = "0.0001") BigDecimal amount
) {}
