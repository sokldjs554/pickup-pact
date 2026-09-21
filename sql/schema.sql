create table if not exists pickup_commitments (
  id uuid primary key,
  store_id text not null,
  pickup_at timestamptz not null,
  units integer not null check (units > 0),
  lease_token text not null,
  payment_authorized boolean not null,
  state text not null,
  version bigint not null default 0,
  updated_at timestamptz not null default now()
);

create table if not exists outbox_events (
  id uuid primary key,
  aggregate_id uuid not null,
  event_type text not null,
  payload jsonb not null,
  occurred_at timestamptz not null,
  published_at timestamptz
);
create index if not exists idx_outbox_unpublished
  on outbox_events(occurred_at, id)
  where published_at is null;
create index if not exists idx_outbox_aggregate_timeline
  on outbox_events(aggregate_id, occurred_at, id);

create table if not exists ledger_batches (
  event_id text primary key,
  semantic_fingerprint text not null,
  aggregate_id text not null,
  reason text not null,
  created_at timestamptz not null default now()
);
create index if not exists idx_ledger_batches_aggregate_created
  on ledger_batches(aggregate_id, created_at desc);

create table if not exists ledger_entries (
  id bigserial primary key,
  event_id text not null references ledger_batches(event_id),
  account text not null,
  direction text not null check (direction in ('DEBIT','CREDIT')),
  amount numeric(18,2) not null check (amount > 0),
  currency char(3) not null,
  occurred_at timestamptz not null
);
create index if not exists idx_ledger_entries_event
  on ledger_entries(event_id);

create table if not exists reconciliation_run (
  run_id uuid primary key,
  aggregate_id text not null,
  canonical_hash char(64) not null,
  anomaly_count integer not null check (anomaly_count >= 0),
  repair_count integer not null check (repair_count >= 0),
  created_at timestamptz not null default now()
);
create index if not exists idx_reconciliation_run_aggregate_created
  on reconciliation_run(aggregate_id, created_at desc);
