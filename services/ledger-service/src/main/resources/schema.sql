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
create index if not exists idx_ledger_entries_event on ledger_entries(event_id);

create table if not exists ledger_conflicts (
  id bigserial primary key,
  event_id text not null,
  aggregate_id text not null,
  reason text not null,
  existing_fingerprint char(64) not null,
  incoming_fingerprint char(64) not null,
  observed_at timestamptz not null default now(),
  unique(event_id, incoming_fingerprint)
);
create index if not exists idx_ledger_conflicts_observed
  on ledger_conflicts(observed_at desc);
