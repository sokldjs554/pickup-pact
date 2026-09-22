#!/usr/bin/env bash
set -euo pipefail

OUTPUT="${1:-/tmp/commitment-timeline-explain.json}"

docker compose up -d postgres
for attempt in $(seq 1 30); do
  if docker compose exec -T postgres pg_isready -U pickuppact -d pickuppact >/dev/null 2>&1; then
    break
  fi
  if [ "$attempt" -eq 30 ]; then
    echo "postgres did not become ready" >&2
    exit 1
  fi
  sleep 1
done

docker compose exec -T postgres psql -U pickuppact -d pickuppact -v ON_ERROR_STOP=1 <<'SQL'
insert into outbox_events(id, aggregate_id, event_type, payload, occurred_at, published_at)
select
  (
    substr(md5(gs::text || '-id'),1,8) || '-' ||
    substr(md5(gs::text || '-id'),9,4) || '-' ||
    substr(md5(gs::text || '-id'),13,4) || '-' ||
    substr(md5(gs::text || '-id'),17,4) || '-' ||
    substr(md5(gs::text || '-id'),21,12)
  )::uuid,
  case when gs <= 100
    then '00000000-0000-0000-0000-000000001001'::uuid
    else (
      substr(md5(gs::text || '-agg'),1,8) || '-' ||
      substr(md5(gs::text || '-agg'),9,4) || '-' ||
      substr(md5(gs::text || '-agg'),13,4) || '-' ||
      substr(md5(gs::text || '-agg'),17,4) || '-' ||
      substr(md5(gs::text || '-agg'),21,12)
    )::uuid
  end,
  'CommitmentConfirmed',
  '{"source":"plan-verification"}'::jsonb,
  now() - (gs || ' seconds')::interval,
  now()
from generate_series(1, 10100) as gs
on conflict (id) do nothing;

analyze outbox_events;
SQL

cat sql/explain/commitment_timeline.sql |   docker compose exec -T postgres psql -XAt -U pickuppact -d pickuppact > "$OUTPUT"

python - "$OUTPUT" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
plan = json.loads(path.read_text())
text = json.dumps(plan)
assert "idx_outbox_aggregate_timeline" in text, text
root = plan[0]["Plan"]
print("execution plan:", root["Node Type"], "rows=", root.get("Actual Rows"))
print("verified index: idx_outbox_aggregate_timeline")
PY
