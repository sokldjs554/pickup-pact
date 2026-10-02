"""A separately running native PG payment role, including committed response loss."""
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4
import pytest


def test_native_payment_role_exists():
    try:importlib.import_module('demo.route.ha.payment_service')
    except ModuleNotFoundError:pytest.fail('separate PostgreSQL payment role is missing')


@pytest.mark.parametrize('lost_action',['AUTHORIZE','CAPTURE','VOID'])
def test_separate_role_recovers_same_receipt_after_process_restart(tmp_path,lost_action):
    dsn=os.environ.get('PICKUP_PG_TEST_DSN')
    if not dsn:pytest.skip('explicit isolated PostgreSQL required')
    assert os.environ.get('PICKUP_HA_TEST')=='1'
    from psycopg import connect,sql
    from demo.route.payments.http_client import PaymentClient
    schema='pact_http_'+uuid4().hex;token='test-only-token-'+uuid4().hex
    env={**os.environ,'PICKUP_PAYMENT_DSN':dsn,'PICKUP_PAYMENT_SCHEMA':schema,'ROUTE_PAYMENT_TOKEN':token,'PICKUP_PAYMENT_INIT_SCHEMA':'1'}
    process=None;log=(tmp_path/'payment-server.log').open('w')
    def launch():
        ready=tmp_path/(uuid4().hex+'.json')
        p=subprocess.Popen([sys.executable,'-m','demo.route.ha.payment_service','--port','0','--ready-file',str(ready)],env=env,stdout=log,stderr=log)
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            if p.poll() is not None:raise AssertionError('payment process failed: '+(tmp_path/'payment-server.log').read_text())
            if ready.exists():
                try:return p,PaymentClient(json.loads(ready.read_text())['url'],token)
                except json.JSONDecodeError:pass
            time.sleep(.03)
        p.terminate();p.wait(5);raise AssertionError('payment readiness timeout')
    c=dict(action='AUTHORIZE',world_id='w-'+uuid4().hex,order_id='PCT-HTTP',operation_key='approve',authorization_id='AUTH-HTTP',amount_krw=2800,currency='KRW',payment_revision=1,quote_fingerprint='a'*64,card_token='demo-approved')
    try:
        process,client=launch()
        if lost_action=='AUTHORIZE':lost=c
        else:
            client.execute(c)
            lost={k:v for k,v in c.items() if k!='card_token'}|dict(action=lost_action,operation_key=lost_action.lower())
        with pytest.raises(OSError):client.execute(lost,fault='drop_reply')
        process.terminate();process.wait(5)
        process,client=launch()
        receipt=client.operation(lost)
        assert receipt and receipt['ok']
        assert client.execute(lost)==receipt
        snapshot=client.snapshot(c['world_id'],c['order_id'])
        assert snapshot['capture_count']==int(lost_action=='CAPTURE')
        assert snapshot['captured_krw']==(2800 if lost_action=='CAPTURE' else 0)
        assert snapshot['held_krw']==(2800 if lost_action=='AUTHORIZE' else 0)
    finally:
        if process and process.poll() is None:process.terminate();process.wait(5)
        log.close()
        with connect(dsn,autocommit=True) as db:db.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))
