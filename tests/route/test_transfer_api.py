from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from demo.main import app
from demo.route import api as route_api
from demo.route.store import JourneyStore

@pytest.fixture(autouse=True)
def isolated(tmp_path,monkeypatch):
    monkeypatch.setattr(route_api,'store',JourneyStore(tmp_path/'route.sqlite'))


def test_control_and_recovery_work_on_the_current_customer_order():
    c=TestClient(app);s=c.post('/api/route/journeys',json={}).json()
    base=f"/api/route/journeys/{s['id']}"
    p=next(p for p in s['all_plans'] if p['store_id']=='wave')
    def act(action,**extra):
        nonlocal s
        r=c.post(base+'/commands',json=dict(action=action,expected_version=s['version'],request_id=uuid4().hex,**extra))
        assert r.status_code==200,r.text;s=r.json();return r
    act('reserve',quote_id=p['quote_id'])
    r=c.post(base+'/transfer-controls',json=dict(action='fault',fault='after_target_hold',expected_version=s['version'],request_id='fault'))
    assert r.status_code==200,r.text;s=r.json()
    p=next(p for p in s['all_plans'] if p['store_id']=='oat')
    act('transfer',quote_id=p['quote_id'])
    assert s['handoff_pending']
    act('recover')
    assert s['order']['store_id']=='oat' and not s['handoff_pending']


def test_control_validation_and_origin_do_not_mutate():
    c=TestClient(app);s=c.post('/api/route/journeys',json={}).json()
    url=f"/api/route/journeys/{s['id']}/transfer-controls"
    valid=dict(action='fault',fault='none',expected_version=1,request_id='t')
    for change in [dict(fault='invalid'),dict(action='occupy'),dict(world_id='other'),dict(expected_version=True),dict(fault='none',store_id='oat')]:
        r=c.post(url,json={**valid,**change});assert r.status_code==422,r.text
    assert c.post(url,json=valid,headers={'Origin':'https://elsewhere.invalid'}).status_code==403
    assert c.get(f"/api/route/journeys/{s['id']}").json()==s


def test_three_policy_comparison_exposes_equal_and_failed_cases():
    c=TestClient(app)
    r=c.post('/api/route/transfer-comparison',json={})
    assert r.status_code==200,r.text
    result=r.json()
    assert result['policies']==['stay','cancel_reorder','guarded_transfer']
    assert len(result['cases'])==6
    same=next(r for r in result['cases'] if r['scenario']=='normal')
    assert same['cancel_reorder']['cash_due']==same['guarded_transfer']['cash_due']
    assert same['cancel_reorder']['same_order'] is False
    rejected=next(r for r in result['cases'] if r['scenario']=='reject_after_quote')
    assert rejected['cancel_reorder']['has_order'] is False
    assert rejected['guarded_transfer']['original_preserved'] is True


def test_new_customer_flow_loads_recovery_ui_assets():
    c=TestClient(app)
    html=c.get('/').text
    assert 'handoff-ui.js' in html and 'handoff.css' in html
    assert c.get('/route-assets/handoff-ui.js').status_code==200
    assert c.get('/route-assets/handoff.css').status_code==200


def test_already_preparing_is_not_unfairly_cancelled_in_the_baseline():
    c=TestClient(app)
    report=c.post('/api/route/transfer-comparison',json={}).json()
    row=next(r for r in report['cases'] if r['scenario']=='already_preparing')
    assert row['cancel_reorder']['original_preserved']
    assert row['guarded_transfer']['original_preserved']
    assert row['cancel_reorder']['customer_commands']==0


def test_comparison_concurrency_limit_fails_cleanly(monkeypatch):
    from threading import BoundedSemaphore
    from demo.route import api as route_api
    slot=BoundedSemaphore(1);slot.acquire()
    monkeypatch.setattr(route_api,'comparison_slots',slot,raising=False)
    r=TestClient(app).post('/api/route/transfer-comparison',json={})
    assert r.status_code==429,r.text
    assert r.headers.get('Retry-After')=='2'


@pytest.mark.parametrize('intent',[
    {'budget':1000,'milk':'oat','decaf':True},
    {'destination':'park','max_detour':0,'deadline_minutes':3},
    {'drink':'americano','points':2000,'coupon_id':'wave1000'},
    {'deadline_minutes':90,'budget':30000,'max_detour':20,'decaf':True},
])
def test_three_policy_comparison_keeps_infeasible_cases_and_bounds(intent):
    r=TestClient(app).post('/api/route/transfer-comparison',json={'intent':intent})
    assert r.status_code==200,r.text
    report=r.json();assert len(report['cases'])==6
    for row in report['cases']:
        for policy in report['policies']:
            value=row[policy]
            assert 0<=value['held_points']<=2000
            assert value['cash_due']<=intent.get('budget',5500)
            assert value['capture_count']==0


def test_public_controls_cannot_join_or_change_another_visitor_world():
    c=TestClient(app)
    a=c.post('/api/route/journeys',json={}).json();b=c.post('/api/route/journeys',json={}).json()
    assert a['world_id']!=b['world_id']
    assert c.post('/api/route/journeys',json={'world_id':a['world_id']}).status_code==422
    r=c.post(f"/api/route/journeys/{a['id']}/transfer-controls",json={
        'action':'occupy','store_id':'oat','expected_version':a['version'],'request_id':'guest'})
    assert r.status_code==200
    assert r.json()['merchant_capacity']['oat']['used']==1
    assert c.get(f"/api/route/journeys/{b['id']}").json()['merchant_capacity']['oat']['used']==0
