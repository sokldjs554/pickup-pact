create table if not exists ledger_batches (
  event_id text primary key,
  semantic_fingerprint text not null,
  aggregate_id text not null,
  reason text not null,
  created_at timestamptz not null default now()
);

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
