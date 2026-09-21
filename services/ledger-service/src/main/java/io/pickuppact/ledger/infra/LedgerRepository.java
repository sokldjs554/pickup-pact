package io.pickuppact.ledger.infra;

import io.pickuppact.ledger.domain.LedgerBatch;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

import java.sql.Timestamp;
import java.util.Optional;

@Repository
public class LedgerRepository {
    private final JdbcTemplate jdbc;

    public LedgerRepository(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    public Optional<String> fingerprint(String eventId) {
        var rows = jdbc.query(
                "select semantic_fingerprint from ledger_batches where event_id = ?",
                (rs, row) -> rs.getString(1),
                eventId
        );
        return rows.stream().findFirst();
    }

    public boolean appendIfAbsent(LedgerBatch batch) {
        int inserted = jdbc.update(
                """
                insert into ledger_batches(event_id, semantic_fingerprint, aggregate_id, reason)
                values(?,?,?,?)
                on conflict (event_id) do nothing
                """,
                batch.eventId(),
                batch.semanticFingerprint(),
                batch.aggregateId(),
                batch.reason()
        );

        if (inserted == 0) {
            return false;
        }

        for (var entry : batch.entries()) {
            jdbc.update(
                    """
                    insert into ledger_entries(event_id, account, direction, amount, currency, occurred_at)
                    values(?,?,?,?,?,?)
                    """,
                    batch.eventId(),
                    entry.account(),
                    entry.direction().name(),
                    entry.amount(),
                    entry.currency(),
                    Timestamp.from(entry.occurredAt())
            );
        }
        return true;
    }
}
