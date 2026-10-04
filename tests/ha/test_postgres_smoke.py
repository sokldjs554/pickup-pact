"""Small real-database gate before the wider parity and concurrency suite."""
import importlib
import os
from uuid import uuid4
import pytest


def test_native_payment_is_shared_by_two_repository_instances():
    dsn=os.environ.get('PICKUP_PG_TEST_DSN')
    if not dsn: pytest.skip('explicit isolated PostgreSQL test database required')
    assert os.environ.get('PICKUP_HA_TEST')=='1'
    try: module=importlib.import_module('demo.route.ha.payment_repository')
    except ModuleNotFoundError: pytest.fail('native PostgreSQL payment repository is not implemented')
    from psycopg import connect,sql
    schema='pact_smoke_'+uuid4().hex
    repos=[]
    try:
        first=module.PostgresPaymentRepository(dsn,schema=schema);repos.append(first)
        second=module.PostgresPaymentRepository(dsn,schema=schema);repos.append(second)
        c=dict(action='AUTHORIZE',world_id='isolated-world',order_id='PCT-SMOKE',operation_key='approve',authorization_id='AUTH-SMOKE',amount_krw=2800,currency='KRW',payment_revision=1,quote_fingerprint='a'*64,card_token='demo-approved')
        approval=first.execute(c)
        assert second.execute(c)==approval
        assert second.operation('approve')==approval
        assert second.snapshot('isolated-world','PCT-SMOKE')['held_krw']==2800
        assert first.backend=='postgresql' and first.storage_ready()
    finally:
        for repo in repos: repo.close()
        with connect(dsn,autocommit=True) as db: db.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))
