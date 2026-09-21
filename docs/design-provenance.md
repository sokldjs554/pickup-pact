# Design provenance: reused engineering discipline, new project

Pickup Pact is a new repository, domain, architecture and implementation. No previous project source tree was copied into it.

Some engineering **patterns** were intentionally carried forward because they were already useful in earlier portfolio work:

- **ChartWire** → durable/replayable evidence, transactional-outbox thinking, explicit load-limit reporting.
- **AEGIS-SQL** → evaluation-first claims, bounded repair, and refusing to turn unmeasured assumptions into benchmark numbers.
- **CareFlow** → separating a reproducible demo path from production-oriented integration artifacts and stating the boundary clearly.

Pickup Pact applies those habits to a different backend problem: pickup-time commitment integrity across capacity and financial side effects. This lets the portfolio show continuity in engineering judgment without presenting an older project under a new name.
