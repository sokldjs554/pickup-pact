from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from demo.main import app
from demo.route import api as route_api
from demo.route.store import JourneyStore

@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(route_api, 'store', JourneyStore(tmp_path/'benefits.sqlite'))

C=TestClient(app)

def new(**patch):
    r=C.post('/api/route/journeys',json={'coupon_id':'welcome500','points':1000,**patch})
    assert r.status_code==201,r.text
    return r.json()

def act(s,action,**patch):
    return C.post(f"/api/route/journeys/{s['id']}/commands",json={
        'action':action,'expected_version':s['version'],'request_id':uuid4().hex,**patch})

def ok(s,action,**patch):
    r=act(s,action,**patch);assert r.status_code==200,r.text;return r.json()

def quote(s,store='wave'):
    return next(p for p in s['all_plans'] if p['store_id']==store)

def ordered(**patch):
    s=new(**patch);return ok(s,'reserve',quote_id=quote(s)['quote_id'])

def ready(s):
    s=ok(s,'advance',minutes=max(0,s['current_plan']['start_at']-s['clock']))
    s=ok(s,'start');s=ok(s,'advance',minutes=s['order']['ready_at']-s['clock']);return ok(s,'ready')

def test_coupon_then_points_pricing_is_server_authoritative():
    s=new();p=quote(s)
    assert p['pricing']['gross']==4300
    assert p['pricing']['coupon_discount']==500
    assert p['pricing']['points_used']==1000
    assert p['price']==p['pricing']['cash_due']==2800
    assert p['pricing']['coupon_id']=='welcome500'
    assert s['wallet']['available_points']==2000

@pytest.mark.parametrize('points',[-1,2001,True,1.5,'100'])
def test_invalid_or_excess_point_input_rejected(points):
    r=C.post('/api/route/journeys',json={'points':points})
    assert r.status_code==422,r.text


def test_unknown_coupon_and_client_authoritative_discount_rejected():
    for payload in [{'coupon_id':'invented'},{'coupon_discount':999999},{'wallet':{'points':999999}}]:
        assert C.post('/api/route/journeys',json=payload).status_code==422


def test_reserve_holds_benefits_cancel_restores_once_and_reopen_keeps_state():
    s=ordered()
    assert s['wallet']['available_points']==1000 and s['wallet']['held_points']==1000
    assert s['wallet']['held_coupon']=='welcome500'
    s=ok(s,'cancel');s=ok(s,'cancel')
    assert s['wallet']['available_points']==2000 and s['wallet']['held_points']==0
    assert s['wallet']['held_coupon'] is None
    assert s['receipt']['net_paid']==0 and s['receipt']['points_spent']==0
    assert len([e for e in s['events'] if e['type']=='BENEFITS_RELEASED'])==1
    assert JourneyStore(route_api.store.path).get(s['id'])['wallet']==s['wallet']


def test_pickup_spends_and_earns_once_even_concurrent_retries():
    s=ready(ordered());code=s['order']['pickup_code']
    data=dict(action='claim',expected_version=s['version'],request_id='one-claim',pickup_code=code)
    url=f"/api/route/journeys/{s['id']}/commands"
    with ThreadPoolExecutor(max_workers=6) as pool:
        rs=list(pool.map(lambda _:C.post(url,json=data),range(12)))
    assert all(r.status_code==200 for r in rs)
    s=C.get(f"/api/route/journeys/{s['id']}").json()
    s=ok(s,'claim',pickup_code=code)
    assert s['wallet']['available_points']==1028
    assert s['wallet']['held_points']==0
    assert s['wallet']['used_coupons']==['welcome500']
    assert s['receipt']['points_spent']==1000 and s['receipt']['points_earned']==28
    assert s['receipt']['net_paid']==2800 and s['receipt']['capture_count']==1


def test_store_coupon_loss_is_quoted_before_transfer_and_same_order_remains():
    s=ordered(coupon_id='wave1000');oid=s['order']['id']
    assert s['order']['price']==2300
    s=ok(s,'disrupt',store_id='wave',minutes=12)
    target=quote(s,'oat')
    assert target['pricing']['coupon_discount']==0
    assert target['pricing']['coupon_reason']=='STORE_MISMATCH'
    assert target['price']==3700
    s=ok(s,'transfer',quote_id=target['quote_id'])
    assert s['order']['id']==oid and s['order']['price']==3700
    assert s['wallet']['held_coupon'] is None and s['wallet']['held_points']==1000
    t=s['receipt']['transfers'][-1]['data']
    assert t['old_price']==2300 and t['new_price']==3700
    assert t['old_pricing']['coupon_discount']==1000 and t['new_pricing']['coupon_discount']==0


def test_coupon_loss_must_not_bypass_final_cash_budget():
    s=ordered(coupon_id='wave1000',budget=3000)
    s=ok(s,'disrupt',store_id='wave',minutes=12)
    p=quote(s,'oat');assert 'BUDGET' in p['reasons']
    before=deepcopy(s)
    r=act(s,'transfer',quote_id=p['quote_id']);assert r.status_code==409
    after=C.get(f"/api/route/journeys/{s['id']}").json()
    assert after['order']==before['order'] and after['wallet']==before['wallet']


def test_point_deduction_cannot_make_cash_negative():
    s=new(drink='americano',coupon_id='morning10',points=2000,budget=1000)
    p=quote(s,'express')
    # Discount coupon has a 3,000 minimum; points are bounded by payable amount.
    assert p['pricing']['coupon_discount']==0
    assert p['pricing']['points_used']==2000 and p['price']==500


def test_percent_coupon_minimum_and_expiration_and_frozen_hold():
    s=new(coupon_id='morning10',points=0,deadline_minutes=90)
    assert quote(s)['pricing']['coupon_discount']==430
    s=ok(s,'advance',minutes=30)
    assert quote(s)['pricing']['coupon_discount']==0
    assert quote(s)['pricing']['coupon_reason']=='EXPIRED'
    held=ordered(coupon_id='morning10',points=0,deadline_minutes=90)
    held=ok(held,'advance',minutes=30)
    assert held['order']['price']==3870
    assert quote(held)['pricing']['coupon_discount']==430
    held=ok(held,'cancel')
    assert next(c for c in held['wallet']['coupons'] if c['id']=='morning10')['status']=='EXPIRED'


def test_wallets_are_explicitly_journey_scoped_not_shared_accounts():
    a=ordered();b=new()
    assert a['wallet']['scope']==b['wallet']['scope']=='journey_demo'
    assert b['wallet']['available_points']==2000


def test_no_benefits_preserves_old_order_prices():
    s=new(coupon_id=None,points=0)
    assert quote(s)['price']==4300
    s=ok(s,'reserve',quote_id=quote(s)['quote_id'])
    assert s['receipt']['coupon_discount']==0


def test_legacy_persisted_order_does_not_gain_unrequested_discount():
    s=ordered(coupon_id=None,points=0)
    with route_api.store.connection() as db:
        raw=route_api.store._load(db,s['id'])
        raw.pop('wallet',None);raw['intent'].pop('coupon_id',None);raw['intent'].pop('points',None)
        raw['order'].pop('pricing',None)
        route_api.store._save(db,raw)
    recovered=C.get(f"/api/route/journeys/{s['id']}").json()
    assert recovered['order']['price']==4300 and recovered['wallet']['held_points']==0


def test_discount_cap_and_points_clipping_with_small_subtotal():
    from demo.route.benefits import price_quote, initial_wallet
    s=dict(clock=0,wallet=initial_wallet(),intent={'coupon_id':'morning10','points':0})
    assert price_quote(s,'wave',10000)['coupon_discount']==800
    s['intent'].update(coupon_id=None,points=2000)
    p=price_quote(s,'wave',1500)
    assert p['points_used']==1500 and p['cash_due']==0 and p['points_clipped']


def test_transfer_retains_platform_coupon_hold_without_double_point_use():
    s=ordered();s=ok(s,'disrupt',store_id='wave',minutes=12)
    s=ok(s,'transfer',quote_id=quote(s,'oat')['quote_id'])
    assert s['order']['price']==3200 and s['wallet']['held_coupon']=='welcome500'
    assert s['wallet']['held_points']==1000 and s['wallet']['available_points']==1000
    s=ready(s);s=ok(s,'claim',pickup_code=s['order']['pickup_code'])
    assert s['wallet']['available_points']==1032 and s['receipt']['points_spent']==1000


def test_database_failure_rolls_back_coupon_point_and_cash_changes(monkeypatch):
    s=ready(ordered());original=route_api.store._save
    def fail_after_write(db,state):
        original(db,state)
        raise RuntimeError('injected transaction failure')
    monkeypatch.setattr(route_api.store,'_save',fail_after_write)
    with pytest.raises(RuntimeError,match='injected'):
        act(s,'claim',pickup_code=s['order']['pickup_code'])
    after=C.get(f"/api/route/journeys/{s['id']}").json()
    assert after==s
    monkeypatch.setattr(route_api.store,'_save',original)
    finished=ok(s,'claim',pickup_code=s['order']['pickup_code'])
    assert finished['receipt']['net_paid']==2800 and finished['wallet']['available_points']==1028


def test_cancel_start_race_keeps_benefit_hold_in_same_result_as_order():
    s=ordered();s=ok(s,'advance',minutes=s['current_plan']['start_at'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending=[pool.submit(act,s,'cancel'),pool.submit(act,s,'start')]
        assert sorted(f.result().status_code for f in pending)==[200,409]
    now=C.get(f"/api/route/journeys/{s['id']}").json()
    assert now['wallet']['held_points']==(0 if now['order']['state']=='CANCELLED' else 1000)
    assert now['receipt']['points_spent']==0 and now['receipt']['net_paid']==0


def test_benefits_quotes_are_rejected_when_expiry_changes_version():
    s=new(coupon_id='morning10',deadline_minutes=90);old=quote(s)['quote_id']
    s=ok(s,'advance',minutes=30)
    assert act(s,'reserve',quote_id=old).status_code==409
    assert C.get(f"/api/route/journeys/{s['id']}").json()['wallet']['held_points']==0
