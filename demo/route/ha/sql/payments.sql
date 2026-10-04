CREATE TABLE IF NOT EXISTS pact_storage_meta (
    singleton integer PRIMARY KEY CHECK(singleton=1),
    version integer NOT NULL CHECK(version=1)
);
INSERT INTO pact_storage_meta VALUES (1,1) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS payment_settings(name text PRIMARY KEY,value bigint NOT NULL CHECK(value>0));
CREATE TABLE IF NOT EXISTS payment_authorizations(
    authorization_id text PRIMARY KEY,world_id text NOT NULL,order_id text NOT NULL,
    amount_krw bigint NOT NULL CHECK(amount_krw BETWEEN 1 AND 30000),
    currency text NOT NULL CHECK(currency='KRW'),
    payment_revision bigint NOT NULL CHECK(payment_revision BETWEEN 1 AND 10000),
    quote_fingerprint text NOT NULL CHECK(quote_fingerprint ~ '^[0-9a-f]{64}$'),
    card_token text NOT NULL CHECK(card_token IN ('demo-approved','demo-declined')),
    status text NOT NULL CHECK(status IN ('AUTHORIZED','CAPTURED','VOIDED')),
    created_at double precision NOT NULL,updated_at double precision NOT NULL
);
CREATE INDEX IF NOT EXISTS payment_order ON payment_authorizations(world_id,order_id);
CREATE TABLE IF NOT EXISTS payment_commands(
    operation_key text PRIMARY KEY,fingerprint text NOT NULL,result jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS payment_transactions(
    seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,id text NOT NULL UNIQUE,
    authorization_id text NOT NULL REFERENCES payment_authorizations(authorization_id),
    world_id text NOT NULL,order_id text NOT NULL,
    kind text NOT NULL CHECK(kind IN ('AUTHORIZE','CAPTURE','VOID')),
    amount_krw bigint NOT NULL CHECK(amount_krw BETWEEN 1 AND 30000),at double precision NOT NULL,
    UNIQUE(authorization_id,kind)
);
CREATE UNIQUE INDEX IF NOT EXISTS payment_one_capture_per_order
    ON payment_transactions(world_id,order_id) WHERE kind='CAPTURE';
CREATE INDEX IF NOT EXISTS payment_tx_order ON payment_transactions(world_id,order_id,seq);
CREATE TABLE IF NOT EXISTS payment_outbox(
    event_id text PRIMARY KEY,world_id text NOT NULL,order_id text NOT NULL,
    revision bigint NOT NULL,body text NOT NULL,delivered boolean NOT NULL DEFAULT false,
    attempts integer NOT NULL DEFAULT 0 CHECK(attempts>=0),
    next_at timestamptz NOT NULL,copies integer NOT NULL DEFAULT 1 CHECK(copies IN (1,2)),
    lease_owner text,lease_version bigint NOT NULL DEFAULT 0,lease_until timestamptz
);
CREATE INDEX IF NOT EXISTS payment_outbox_due ON payment_outbox(next_at,revision)
    WHERE delivered=false AND attempts<8;
CREATE TABLE IF NOT EXISTS payment_faults(
    world_id text NOT NULL,operation_key text NOT NULL,mode text NOT NULL,
    PRIMARY KEY(world_id,operation_key,mode)
);
