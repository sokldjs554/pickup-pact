EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
SELECT event_id, event_type, occurred_at, payload_json
FROM outbox_event
WHERE aggregate_id = 'order-1001'
ORDER BY occurred_at, event_id;
