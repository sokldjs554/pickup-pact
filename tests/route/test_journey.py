"""Customer outcomes and safety boundaries; real API/store, no mocked planner."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
from fastapi.testclient import TestClient
from demo.main import app

INTENT = dict(destination='office', deadline_minutes=16, drink='latte', milk='regular',
              decaf=False, budget=5500, max_detour=5, priority='arrival')

def new(**changes):
    r = TestClient(app).post('/api/route/journeys', json={**INTENT, **changes})
    assert r.status_code == 201, r.text
    return r.json()

def command(s, action, **kw):
    return TestClient(app).post(f"/api/route/journeys/{s['id']}/commands", json={
        'action': action, 'expected_version': s['version'], 'request_id': uuid4().hex, **kw,
    })

def accepted(s, action, **kw):
    r = command(s, action, **kw); assert r.status_code == 200, r.text
    return r.json()

def ordered(**kw):
    s = new(**kw)
    return accepted(s, 'reserve', quote_id=s['recommendations'][0]['quote_id'])

def test_time_goal_excludes_closest_cafe_when_queue_breaks_deadline():
    s = new(deadline_minutes=12)
    assert s['recommendations'][0]['store_id'] == 'wave'
    nearest = min(s['all_plans'], key=lambda p: p['walk_to'])
    assert nearest['store_id'] == 'corner'
    assert not nearest['feasible']
    assert 'DEADLINE' in nearest['reasons']

def test_preferences_and_budget_never_silently_relaxed():
    s = new(decaf=True, milk='oat')
    assert s['recommendations']
    for p in s['recommendations']:
        assert p['decaf'] and p['milk'] == 'oat' and p['price'] <= 5500
    s = new(budget=3000, milk='oat')
    assert s['recommendations'] == []
    assert s['order'] is None

def test_busy_store_rescue_keeps_one_order_and_one_authorization():
    s = ordered(); oid = s['order']['id']; source = s['order']['store_id']
    s = accepted(s, 'disrupt', store_id=source, minutes=12)
    assert s['risk']['needs_attention']
    p = s['recommendations'][0]
    assert p['store_id'] != source
    s = accepted(s, 'transfer', quote_id=p['quote_id'])
    assert s['order']['id'] == oid and s['order']['store_id'] != source
    assert s['order']['price'] <= s['intent']['budget']
    assert sum(e['type'] == 'PAYMENT_AUTHORIZED' for e in s['events']) == 1
    assert sum(e['type'] == 'ORDER_TRANSFERRED' for e in s['events']) == 1
    assert s['receipt']['capture_count'] == 0
    assert not s['risk']['needs_attention']

def test_stale_quote_is_rejected_without_moving_order():
    s = ordered(); original = s['order']['store_id']; old = s['recommendations'][0]['quote_id']
    s = accepted(s, 'disrupt', store_id=original, minutes=12)
    r = command(s, 'transfer', quote_id=old)
    assert r.status_code == 409
    assert TestClient(app).get(f"/api/route/journeys/{s['id']}").json()['order']['store_id'] == original

def test_order_cannot_move_after_manufacturing_started():
    s = ordered()
    s = accepted(s, 'advance', minutes=max(0, s['order']['plan']['start_at'] - s['clock']))
    s = accepted(s, 'start')
    s = accepted(s, 'disrupt', store_id=s['order']['store_id'], minutes=12)
    r = command(s, 'transfer', quote_id='anything')
    assert r.status_code == 409 and r.json()['detail']['code'] == 'ALREADY_PREPARING'
    assert s['order']['state'] == 'PREPARING'

def test_idempotency_replays_but_key_reuse_with_other_payload_conflicts():
    s = new(); payload = dict(action='reserve', expected_version=s['version'],
        request_id='fixed-key', quote_id=s['recommendations'][0]['quote_id'])
    url = f"/api/route/journeys/{s['id']}/commands"; c = TestClient(app)
    first = c.post(url, json=payload); second = c.post(url, json=payload)
    assert first.status_code == second.status_code == 200
    assert second.json()['duplicate'] is True
    assert len(first.json()['events']) == len(second.json()['events'])
    wrong = c.post(url, json={**payload, 'quote_id': 'modified'})
    assert wrong.status_code == 409

def test_pickup_code_is_order_bound_and_capture_occurs_once():
    s = ordered(); oid = s['order']['id']
    s = accepted(s, 'advance', minutes=max(0, s['order']['plan']['start_at'] - s['clock']))
    s = accepted(s, 'start')
    assert command(s, 'claim', pickup_code='123456').status_code == 409
    s = accepted(s, 'advance', minutes=s['order']['ready_at'] - s['clock'])
    s = accepted(s, 'ready')
    assert command(s, 'claim', pickup_code='wrong-code').status_code == 409
    code = s['order']['pickup_code']; s = accepted(s, 'claim', pickup_code=code)
    assert s['order']['state'] == 'PICKED_UP' and s['order']['id'] == oid
    assert s['receipt']['capture_count'] == 1 and s['receipt']['net_paid'] == s['order']['price']
    s2 = accepted(s, 'claim', pickup_code=code)
    assert s2['receipt']['capture_count'] == 1

def test_concurrent_transfer_and_start_have_one_winner():
    s = ordered(); s = accepted(s, 'advance', minutes=max(0,s['order']['plan']['start_at']-s['clock']))
    alternate = next(p for p in s['recommendations'] if p['store_id'] != s['order']['store_id'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        fs = [pool.submit(command,s,'start'), pool.submit(command,s,'transfer',quote_id=alternate['quote_id'])]
        statuses = sorted(f.result().status_code for f in fs)
    assert statuses == [200,409]
    now = TestClient(app).get(f"/api/route/journeys/{s['id']}").json()
    assert now['order']['state'] in {'RESERVED','PREPARING'}
    assert sum(e['type']=='PAYMENT_AUTHORIZED' for e in now['events']) == 1

def test_sessions_are_isolated_and_customer_can_cancel_before_preparation():
    a,b = ordered(), ordered()
    a = accepted(a, 'cancel')
    assert a['receipt']['net_paid'] == 0 and a['order']['state'] == 'CANCELLED'
    assert TestClient(app).get(f"/api/route/journeys/{b['id']}").json()['order']['state'] == 'RESERVED'

def test_invalid_shape_and_cross_origin_do_not_change_state():
    c = TestClient(app)
    for patch in [{'budget':-1},{'deadline_minutes':True},{'milk':'unknown'},{'drink':'unknown'}, {'decaf':'yes'}]:
        assert c.post('/api/route/journeys', json={**INTENT, **patch}).status_code == 422
    s = ordered()
    r = c.post(f"/api/route/journeys/{s['id']}/commands", json=dict(action='cancel',expected_version=s['version'],request_id='csrf'),headers={'Origin':'https://elsewhere.invalid'})
    assert r.status_code == 403
    assert c.get(f"/api/route/journeys/{s['id']}").json()['version'] == s['version']

def test_active_reservation_does_not_push_its_start_time_forward_on_each_tick():
    s=ordered(); promised=s['order']['plan']['start_at']
    s=accepted(s,'advance',minutes=promised)
    assert s['current_plan']['start_at']==promised
    assert command(s,'start').status_code==200

def test_twenty_four_simultaneous_retries_record_one_order():
    s=new();url=f"/api/route/journeys/{s['id']}/commands"
    data=dict(action='reserve',expected_version=s['version'],request_id='parallel',quote_id=s['recommendations'][0]['quote_id'])
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows=list(pool.map(lambda _:TestClient(app).post(url,json=data),range(24)))
    assert all(r.status_code==200 for r in rows)
    assert sum(not r.json()['duplicate'] for r in rows)==1
    assert all(r.json()['receipt']['authorization_count']==1 for r in rows)

def test_existing_order_survives_an_independent_store_connection():
    from demo.route.api import store
    from demo.route.store import JourneyStore
    s=ordered();reopened=JourneyStore(store.path).get(s['id'])
    assert reopened['order']==s['order']
    assert reopened['version']==s['version'] and reopened['events']==s['events']

def test_request_without_json_and_unknown_session_fail_cleanly():
    c=TestClient(app)
    assert c.post('/api/route/journeys',content='{}',headers={'Content-Type':'text/plain'}).status_code==415
    assert c.get('/api/route/journeys/'+uuid4().hex).status_code==404

def test_no_silent_budget_bypass_by_submitting_excluded_quote():
    s=new(budget=4500)
    excluded=next(p for p in s['all_plans'] if 'BUDGET' in p['reasons'])
    r=command(s,'reserve',quote_id=excluded['quote_id'])
    assert r.status_code==409
    assert TestClient(app).get(f"/api/route/journeys/{s['id']}").json()['order'] is None
