CREATE TABLE IF NOT EXISTS journeys(
  id text PRIMARY KEY,version bigint NOT NULL CHECK(version>=1),
  body jsonb NOT NULL CHECK(jsonb_typeof(body)='object'));
CREATE TABLE IF NOT EXISTS journey_requests(
  journey_id text NOT NULL REFERENCES journeys(id),channel text NOT NULL,request_id text NOT NULL,
  fingerprint text NOT NULL,operation_id text REFERENCES ha_operations(id),
  PRIMARY KEY(journey_id,channel,request_id));
CREATE TABLE IF NOT EXISTS authorization_refs(
  authorization_id text PRIMARY KEY,world_id text NOT NULL,order_id text NOT NULL);
CREATE TABLE IF NOT EXISTS notification_inbox(
  event_id text PRIMARY KEY,body_sha256 text NOT NULL,world_id text NOT NULL,order_id text NOT NULL,
  revision bigint NOT NULL,body jsonb NOT NULL,received_at timestamptz NOT NULL DEFAULT clock_timestamp());
CREATE TABLE IF NOT EXISTS notification_hints(
  world_id text NOT NULL,order_id text NOT NULL,revision bigint NOT NULL,PRIMARY KEY(world_id,order_id));
CREATE TABLE IF NOT EXISTS recovery_nodes(node_id text PRIMARY KEY,seen_at timestamptz NOT NULL);
