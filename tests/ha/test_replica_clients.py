"""Real HTTP replica clients retain the operation key when a peer disappears."""
from contextlib import contextmanager
import importlib
import json
import os
import secrets
import subprocess
import sys
import time
from threading import Event
from demo.route.payments.runtime import _wait_ready_json
import pytest


def client_type():
    try:
        return importlib.import_module('demo.route.ha.replica_clients').ReplicaPaymentClient
    except ModuleNotFoundError:
        pytest.fail('replica payment transport is not implemented')


@contextmanager
def providers(root):
    token=secrets.token_hex(32)
    processes=[]
    urls=[]
    try:
        for index in range(2):
            marker=root/('ready-'+str(index)+'.json')
            process=subprocess.Popen([sys.executable,'-m','demo.route.payments.http_server',
                '--directory',str(root/'common-test-db'),'--ready-file',str(marker)],
                env={**os.environ,'ROUTE_PAYMENT_TOKEN':token},stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            processes.append(process)
            ready=_wait_ready_json(marker,process,Event(),time.monotonic()+10)
            assert ready['pid']==process.pid
            urls.append(ready['url'])
        yield token,urls,processes
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:process.wait(5)
                except subprocess.TimeoutExpired:process.kill();process.wait(5)


def approval():
    return dict(action='AUTHORIZE',world_id='world',order_id='PCT-order',operation_key='original-approve',
        authorization_id='AUTH-original',amount_krw=2800,currency='KRW',payment_revision=1,
        quote_fingerprint='a'*64,card_token='demo-approved')


def test_surviving_peer_reuses_original_approval_and_captures_once(tmp_path):
    Client=client_type()
    with providers(tmp_path) as (token,urls,peers):
        client=Client(urls,token,timeout=.5)
        request=approval()
        approved=client.execute(request)
        peers[0].terminate();peers[0].wait(5)
        assert client.operation(request)==approved
        capture={k:v for k,v in request.items() if k!='card_token'}|dict(action='CAPTURE',operation_key='original-capture')
        first=client.execute(capture)
        assert client.execute(capture)==first
        proof=client.snapshot('world','PCT-order')
        assert proof['capture_count']==1 and proof['captured_krw']==2800 and proof['held_krw']==0


def test_unknown_write_is_not_silently_retried_as_a_new_transaction(tmp_path):
    Client=client_type()
    with providers(tmp_path) as (token,urls,peers):
        client=Client(urls,token,timeout=.5)
        request=approval()
        with pytest.raises(OSError):client.execute(request,fault='drop_reply')
        stored=client.operation(request)
        assert stored['operation_key']=='original-approve'
        proof=client.snapshot('world','PCT-order')
        assert len(proof['transactions'])==1 and proof['held_krw']==2800 and proof['capture_count']==0
        for peer in peers:peer.terminate();peer.wait(5)
        with pytest.raises(OSError):client.operation(request)
        with pytest.raises(OSError):client.health()


def test_replica_configuration_rejects_unbounded_duplicate_or_remote_origins():
    Client=client_type()
    for urls in [[],['http://127.0.0.1:12']*2,['http://outside.example'],['http://127.0.0.1:'+str(p) for p in range(4)]]:
        with pytest.raises(ValueError):Client(urls,'t'*32)


def test_role_settings_validate_every_explicit_failover_origin():
    from demo.route.ha.settings import RoleSettings
    config={'PICKUP_ORDER_DSN':'dbname=test','PICKUP_NODE_ID':'node',
        'ROUTE_MERCHANT_URL':'http://127.0.0.1:19001','ROUTE_MERCHANT_TOKEN':'m'*32,
        'ROUTE_PAYMENT_URL':'http://127.0.0.1:19002','ROUTE_PAYMENT_TOKEN':'p'*32,
        'ROUTE_PAYMENT_NOTIFY_SECRET':'s'*32,
        'ROUTE_PAYMENT_FAILOVER_URLS':'["http://127.0.0.1:19003"]'}
    parsed=RoleSettings.from_env(config)
    assert getattr(parsed,'payment_failovers',None)==('http://127.0.0.1:19003',)
    for value in ['["http://outside.example"]','["http://127.0.0.1:19002"]','"http://127.0.0.1:3"']:
        with pytest.raises(ValueError):RoleSettings.from_env(config|{'ROUTE_PAYMENT_FAILOVER_URLS':value})
