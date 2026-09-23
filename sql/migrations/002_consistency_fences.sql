-- Apply after schema.sql, before starting writers. Idempotent for fresh and existing schemas.
alter table pickup_commitments add column if not exists preparation_command_id text;
alter table pickup_commitments add column if not exists preparation_authorized_at timestamptz;

-- Server-owned evidence head: revision changes only when distinct semantic facts are added.
create table if not exists projection_evidence_heads (
  aggregate_id text primary key,
  revision bigint not null default 0 check (revision >= 0),
  evidence_hash char(64) not null
);
create table if not exists projection_evidence_events (
  aggregate_id text not null references projection_evidence_heads(aggregate_id),
  event_id text not null,
  semantic_hash char(64) not null,
  envelope jsonb not null,
  primary key(aggregate_id, event_id, semantic_hash)
);
alter table commitment_projection add column if not exists source_revision bigint not null default 0;
alter table commitment_projection add column if not exists evidence_hash char(64);
