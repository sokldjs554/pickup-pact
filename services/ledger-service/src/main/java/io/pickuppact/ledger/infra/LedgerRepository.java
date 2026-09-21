package io.pickuppact.ledger.infra;

import io.pickuppact.ledger.domain.LedgerBatch;
import io.pickuppact.ledger.domain.LedgerBatchSummary;
import io.pickuppact.ledger.domain.LedgerConflict;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

import java.sql.Timestamp;
import java.util.List;
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
        if (inserted == 0) return false;

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

    public void recordConflict(LedgerBatch batch, String existingFingerprint) {
        jdbc.update(
                """
                insert into ledger_conflicts(
                  event_id, aggregate_id, reason, existing_fingerprint, incoming_fingerprint
                )
                values(?,?,?,?,?)
                on conflict (event_id, incoming_fingerprint) do nothing
                """,
                batch.eventId(),
                batch.aggregateId(),
                batch.reason(),
                existingFingerprint,
                batch.semanticFingerprint()
        );
    }

    public List<LedgerBatchSummary> history(String aggregateId, int limit) {
        return jdbc.query(
                """
                select event_id, aggregate_id, reason, created_at
                from ledger_batches
                where aggregate_id = ?
                order by created_at desc, event_id desc
                limit ?
                """,
                (rs, rowNum) -> new LedgerBatchSummary(
                        rs.getString("event_id"),
                        rs.getString("aggregate_id"),
                        rs.getString("reason"),
                        rs.getTimestamp("created_at").toInstant()
                ),
                aggregateId,
                limit
        );
    }

    public List<LedgerConflict> conflicts(int limit) {
        return jdbc.query(
                """
                select event_id, aggregate_id, reason,
                       existing_fingerprint, incoming_fingerprint, observed_at
                from ledger_conflicts
                order by observed_at desc, id desc
                limit ?
                """,
                (rs, rowNum) -> new LedgerConflict(
                        rs.getString("event_id"),
                        rs.getString("aggregate_id"),
                        rs.getString("reason"),
                        rs.getString("existing_fingerprint"),
                        rs.getString("incoming_fingerprint"),
                        rs.getTimestamp("observed_at").toInstant()
                ),
                limit
        );
    }
}
