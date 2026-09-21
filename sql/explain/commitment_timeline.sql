EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
SELECT
  id AS event_id,
  event_type,
  occurred_at,
  payload
FROM outbox_events
WHERE aggregate_id = '00000000-0000-0000-0000-000000001001'::uuid
ORDER BY occurred_at, id;
