CREATE TABLE IF NOT EXISTS ha_operations(
    id text PRIMARY KEY,order_id text NOT NULL,request_key text NOT NULL,
    fingerprint text NOT NULL,payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object'),
    status text NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','COMPLETED')),
    phase_version bigint NOT NULL DEFAULT 0 CHECK(phase_version>=0),
    lease_owner text,lease_version bigint NOT NULL DEFAULT 0 CHECK(lease_version>=0),
    lease_until timestamptz,next_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(order_id,request_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS ha_one_active_operation ON ha_operations(order_id) WHERE status='PENDING';
CREATE INDEX IF NOT EXISTS ha_operation_due ON ha_operations(next_at,id) WHERE status='PENDING';
