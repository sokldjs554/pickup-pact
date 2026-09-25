"""Real loopback sockets and a separate process, never ASGI/mock transports."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from uuid import uuid4
import httpx
import pytest
from demo.route.store import JourneyStore
from test_transfer_durability import ordered, configure, command, plan


@contextmanager
def service(tmp_path, crash_phase=None, port=0):
    ready=tmp_path/('ready-'+uuid4().hex+'.json')
    token=secrets.token_hex(24)
    args=[sys.executable,'-m','demo.route.merchant_http','--directory',str(tmp_path/'merchants'),
          '--port',str(port),'--ready-file',str(ready)]
    if crash_phase:args+=['--crash-phase',crash_phase]
    with (tmp_path/'http-service.log').open('a') as log:
        child=subprocess.Popen(args,env={**os.environ,'ROUTE_MERCHANT_TOKEN':token},stdout=log,stderr=log)
        try:
            until=time.monotonic()+8
            while not ready.exists() and child.poll() is None and time.monotonic()<until:time.sleep(.02)
            assert ready.exists(),(child.poll(),(tmp_path/'http-service.log').read_text())
            yield json.loads(ready.read_text())['url'], token, child
        finally:
            if child.poll() is None:
                child.terminate()
                try:child.wait(5)
                except subprocess.TimeoutExpired:child.kill();child.wait()


def client(url,token):
    from demo.route.merchant_http import HttpMerchantFleet
    return HttpMerchantFleet(url,token,timeout=.5)


def test_separate_server_survives_client_reopen_and_replays_receipt(tmp_path):
    with service(tmp_path) as (url,token,child):
        fleet=client(url,token);assert child.pid!=os.getpid()
        args=dict(world='world',order_id='PCT-123',generation=0,operation_id='admit-1',action='ADMIT')
        a=fleet.execute('oat',**args);b=client(url,token).execute('oat',**args)
        assert a==b and a['ok']
        assert fleet.snapshot('world')['oat']['used']==1


def test_commit_then_actual_dropped_http_response_is_recoverable(tmp_path):
    with service(tmp_path) as (url,token,child):
        st=JourneyStore(tmp_path/'orders.sqlite',fleet=client(url,token));s=ordered(st)
        s=configure(st,s,'after_target_hold')
        s=command(st,s,'transfer',quote_id=plan(s,'oat')['quote_id'])
        assert s['handoff_pending'] and s['handoff']['phase']=='HOLD'
        assert st.fleet.snapshot(s['world_id'])['oat']['used']==1
        from demo.route.recovery_worker import RecoveryWorker
        RecoveryWorker(st).run_once(now=s['handoff']['recovery']['next_retry_at']+.01)
        done=st.get(s['id']);assert done['order']['store_id']=='oat' and not done['handoff_pending']
        assert len(done['receipt']['transfers'])==1 and done['receipt']['authorization_count']==1
        assert child.poll() is None


def test_two_independent_coordinators_use_one_shared_http_seat(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    with service(tmp_path) as (url,token,_):
        a=JourneyStore(tmp_path/'a.sqlite',fleet=client(url,token));b=JourneyStore(tmp_path/'b.sqlite',fleet=client(url,token))
        sa=ordered(a);sb=ordered(b,world=sa['world_id'])
        with ThreadPoolExecutor(max_workers=2) as ex:
            fs=[ex.submit(command,st,s,'transfer',quote_id=plan(s,'oat')['quote_id']) for st,s in ((a,sa),(b,sb))]
            rows=[f.result() for f in fs]
        assert sorted(r['handoff']['status'] for r in rows)==['COMPLETED','REJECTED']
        assert a.fleet.snapshot(sa['world_id'])['oat']['used']==1


def test_authentication_and_bad_command_do_not_mutate_merchant(tmp_path):
    with service(tmp_path) as (url,token,_),httpx.Client(trust_env=False,timeout=2) as c:
        p=dict(shop='oat',world='world',order_id='PCT-123',generation=0,operation_id='x',action='ADMIT')
        assert c.post(url+'/v1/command',json=p).status_code==403
        assert c.post(url+'/v1/command',json={**p,'generation':True},headers={'Authorization':'Bearer '+token}).status_code==422
        assert c.post(url+'/v1/command',content='{' ,headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'}).status_code==400
        assert client(url,token).snapshot('world')['oat']['used']==0


@pytest.mark.parametrize('phase',['HOLD','RELEASE_SOURCE','ACTIVATE'])
def test_server_dies_after_commit_and_restarts_before_auto_recovery(tmp_path,phase):
    from demo.route.recovery_worker import RecoveryWorker
    with service(tmp_path,crash_phase=phase) as (url,token,child):
        port=int(url.rsplit(':',1)[1]);st=JourneyStore(tmp_path/'orders.sqlite',fleet=client(url,token));s=ordered(st)
        original=s['order']['id']
        s=command(st,s,'transfer',quote_id=plan(s,'oat')['quote_id'])
        child.wait(4);assert child.returncode==91
        assert s['handoff_pending'] and s['merchant_connection']=='unavailable'
    with service(tmp_path,port=port) as (url,token,_):
        st=JourneyStore(tmp_path/'orders.sqlite',fleet=client(url,token))
        RecoveryWorker(st).run_once(now=s['handoff']['recovery']['next_retry_at']+.01)
        done=st.get(s['id']);assert not done['handoff_pending'] and done['order']['store_id']=='oat'
        assert done['order']['id']==original and done['wallet']['held_points']==1000
        assert len(done['receipt']['transfers'])==1


def test_unavailable_transport_does_not_break_reading_existing_order(tmp_path):
    with service(tmp_path) as (url,token,child):
        st=JourneyStore(tmp_path/'orders.sqlite',fleet=client(url,token));s=ordered(st)
        child.terminate();child.wait(4)
        read=st.get(s['id'])
        assert read['order']==s['order'] and read['wallet']==s['wallet']
        assert read['merchant_connection']=='unavailable'


@pytest.mark.parametrize('patch',[{'shop':[]},{'action':{}},{'world':[]},{'order_id':[]},{'operation_id':{}},{'transfer_id':3}])
def test_malformed_command_types_are_json_4xx_not_socket_errors(tmp_path,patch):
    with service(tmp_path) as (url,token,_),httpx.Client(trust_env=False,timeout=2) as c:
        payload=dict(shop='oat',world='world',order_id='order',generation=0,operation_id='id',action='ADMIT')
        response=c.post(url+'/v1/command',json={**payload,**patch},headers={'Authorization':'Bearer '+token})
        assert response.status_code==422
        assert client(url,token).snapshot('world')['oat']['used']==0
