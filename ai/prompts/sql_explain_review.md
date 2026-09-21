# PostgreSQL EXPLAIN review prompt

Input is `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` plus table/index definitions.

Return JSON with:

- `observations`: facts directly visible in the plan;
- `hypotheses`: possible causes, each with evidence;
- `candidate_changes`: index/query/schema changes;
- `verification`: exact plan or benchmark that would confirm the change;
- `do_not_claim`: improvements that cannot be asserted from current evidence.

Never invent latency improvements. Distinguish planning time, execution time, row-estimation error, buffer hits/reads, sorts, and scans.
