CREATE TABLE IF NOT EXISTS merchant_seats(
  shop text NOT NULL,world text NOT NULL,order_id text NOT NULL,
  generation bigint NOT NULL CHECK(generation>=0),phase text NOT NULL
    CHECK(phase IN ('RESERVED','FROZEN','HELD','PREPARING','READY','RELEASED','ABORTED','CANCELLED','CLAIMED')),
  transfer_id text NOT NULL,PRIMARY KEY(shop,world,order_id));
CREATE INDEX IF NOT EXISTS merchant_capacity ON merchant_seats(shop,world,phase);
CREATE TABLE IF NOT EXISTS merchant_policy(
  shop text NOT NULL,world text NOT NULL,accepting boolean NOT NULL,PRIMARY KEY(shop,world));
CREATE TABLE IF NOT EXISTS merchant_receipts(
  seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  shop text NOT NULL,world text NOT NULL,command_id text NOT NULL,
  fingerprint text NOT NULL,result jsonb NOT NULL,order_id text NOT NULL,
  action text NOT NULL,generation bigint NOT NULL,UNIQUE(shop,world,command_id));
CREATE INDEX IF NOT EXISTS merchant_order_evidence ON merchant_receipts(shop,world,order_id,seq DESC);
CREATE TABLE IF NOT EXISTS merchant_reply_loss(
  world text NOT NULL,command_id text NOT NULL,PRIMARY KEY(world,command_id));
