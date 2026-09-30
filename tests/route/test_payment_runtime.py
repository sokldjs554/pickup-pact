import sqlite3
import time
from uuid import uuid4
from fastapi.testclient import TestClient
from demo.main import app
from demo.route import api
from demo.route.store import JourneyStore


def test_runtime_connects_both_independent_servers_and_preserves_pending(monkeypatch,tmp_path):
    st=JourneyStore(tmp_path/'orders.sqlite');monkeypatch.setattr(api,'store',st)
    with TestClient(app) as client:
        runtime=client.get('/api/route/runtime').json()
        assert runtime['payment_transport']=='http' and runtime['payment_connection']=='available'
        assert runtime['payment_storage_ready'] is True
        s=client.post('/api/route/journeys',json={'payment_scenario':'authorize_reply_lost','points':1000,'coupon_id':'welcome500'}).json()
        assert s['protocol_version']==2
        p=next(p for p in s['all_plans'] if p['store_id']=='wave')
        s=client.post(f"/api/route/journeys/{s['id']}/commands",json=dict(action='reserve',expected_version=s['version'],request_id='runtime',quote_id=p['quote_id'])).json()
        assert s['order'] is None and s['handoff_pending']
        assert s['payment']['state']=='CONFIRMING_APPROVAL'
        oid=s['pending_order_id']
        until=time.monotonic()+8
        while time.monotonic()<until:
            s=client.get(f"/api/route/journeys/{s['id']}").json()
            if not s['handoff_pending']:break
            time.sleep(.1)
        assert s['order']['id']==oid and s['payment']['state']=='AUTHORIZED'
        with sqlite3.connect(st.path) as db:
            assert db.execute('select count(*) from payment_inbox').fetchone()[0]>=1
    assert st.payment_gateway is None


def test_public_payment_fields_are_strict_and_have_no_url_or_real_card(monkeypatch,tmp_path):
    monkeypatch.setattr(api,'store',JourneyStore(tmp_path/'orders.sqlite'))
    with TestClient(app) as client:
        for body in [{'payment_card':'actual-card'},{'payment_scenario':'kill'},{'callback_url':'http://elsewhere'}, {'card_number':'1234'}]:
            assert client.post('/api/route/journeys',json=body).status_code==422
