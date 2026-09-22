EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
SELECT
  id,
  pickup_at,
  units,
  state
FROM pickup_commitments
WHERE store_id = 'store-plan-target'
  AND state IN ('HELD', 'CONFIRMED', 'AT_RISK')
  AND pickup_at >= now()
  AND pickup_at < now() + interval '2 hours'
ORDER BY pickup_at, id
LIMIT 100;
