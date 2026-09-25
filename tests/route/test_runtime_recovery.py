"""The shipped ASGI lifespan must use the HTTP boundary and unattended recovery."""
import time
from uuid import uuid4
from fastapi.testclient import TestClient
from demo.main import app
from demo.route import api as route_api
from demo.route.store import JourneyStore


def test_public_runtime_recovers_without_a_recover_command(tmp_path,monkeypatch):
    monkeypatch.setattr(route_api,'store',JourneyStore(tmp_path/'runtime.sqlite'))
    with TestClient(app) as c:
        runtime=c.get('/api/route/runtime')
        assert runtime.status_code==200, runtime.text
        assert runtime.json()['merchant_transport']=='http'
        assert runtime.json()['automatic_recovery'] is True
        s=c.post('/api/route/journeys',json={'coupon_id':'welcome500','points':1000}).json()
        def cmd(action,**kw):
            nonlocal s
            r=c.post(f"/api/route/journeys/{s['id']}/commands",json=dict(action=action,expected_version=s['version'],request_id=uuid4().hex,**kw))
            assert r.status_code==200,r.text;s=r.json()
        def q(shop):return next(p['quote_id'] for p in s['all_plans'] if p['store_id']==shop)
        cmd('reserve',quote_id=q('wave'));oid=s['order']['id']
        r=c.post(f"/api/route/journeys/{s['id']}/transfer-controls",json=dict(action='fault',fault='after_target_hold',expected_version=s['version'],request_id=uuid4().hex))
        assert r.status_code==200,r.text;s=r.json()
        cmd('transfer',quote_id=q('oat'))
        assert s['handoff_pending'] and s['automatic_recovery_enabled']
        deadline=time.monotonic()+8
        while time.monotonic()<deadline and s['handoff_pending']:
            time.sleep(.1);s=c.get('/api/route/journeys/'+s['id']).json()
        assert not s['handoff_pending']
        assert s['order']['id']==oid and s['order']['store_id']=='oat'
        assert len(s['receipt']['transfers'])==1 and s['wallet']['held_points']==1000
    assert not route_api.store.automatic_recovery_enabled


def test_supervisor_restarts_merchant_and_worker_finishes_saved_operation(tmp_path):
    from demo.route.runtime import MerchantProcess
    from demo.route.recovery_worker import RecoveryWorker
    from test_transfer_durability import ordered,configure,command,plan
    runtime=MerchantProcess(str(tmp_path/'fleet'))
    with runtime as fleet:
        st=JourneyStore(tmp_path/'orders.sqlite',fleet=fleet)
        s=ordered(st);oid=s['order']['id'];s=configure(st,s,'after_target_hold')
        s=command(st,s,'transfer',quote_id=plan(s,'oat')['quote_id'])
        assert s['handoff_pending']
        old=runtime.process;old.kill();old.wait(3)
        with RecoveryWorker(st,interval=.05):
            until=time.monotonic()+9
            while time.monotonic()<until:
                done=st.get(s['id'])
                if not done['handoff_pending']:break
                time.sleep(.1)
        assert runtime.process.pid!=old.pid and runtime.process.poll() is None
        assert done['order']['id']==oid and done['order']['store_id']=='oat'
        assert not done['handoff_pending'] and len(done['receipt']['transfers'])==1
    assert runtime.process.poll() is not None
