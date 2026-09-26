"""Rules that make a synthetic cross-store substitution explicit, not assumed."""
from copy import deepcopy
from uuid import uuid4
import pytest
from demo.route.api import Intent
from demo.route.store import JourneyStore, Conflict


def act(st,s,action,**kw):
    return st.command(s['id'],dict(action=action,expected_version=s['version'],request_id=uuid4().hex,**kw))


def plan(s,shop):
    return next(p for p in s['all_plans'] if p['store_id']==shop)


def order(st,**kw):
    s=st.create(Intent(coupon_id='welcome500',points=1000,**kw).model_dump())
    return act(st,s,'reserve',quote_id=plan(s,'wave')['quote_id'])


def alter(st,s,shop,fn):
    with st.connection() as db:
        data=st._load(db,s['id'])
        fn(next(x for x in data['stores'] if x['id']==shop))
        st._save(db,data)
    return st.get(s['id'])


def test_new_journey_states_substitution_and_cost_responsibility(tmp_path):
    st=JourneyStore(tmp_path/'orders.sqlite');s=order(st)
    terms=plan(s,'oat')['transfer_terms']
    assert terms['eligible'] and terms['version']=='partner-transfer-v1'
    assert terms['source_product']['volume_ml']==terms['target_product']['volume_ml']==360
    assert terms['target_product']['temperature']=='ICED'
    assert '맛' in terms['notice']
    money=terms['funding']
    assert money['customer_cash']==3200 and money['platform_coupon']==500
    assert money['platform_points']==1000 and money['merchant_coupon']==0
    assert money['merchant_receivable']==4700
    assert money['customer_cash']+money['platform_coupon']+money['platform_points']==money['merchant_receivable']


@pytest.mark.parametrize('change,reason',[
    (lambda p:p.update(enabled=False),'PARTNER_DISABLED'),
    (lambda p:p.update(group='unrelated'),'PARTNER_GROUP'),
    (lambda p:p.update(volume_ml=240),'PRODUCT_SPEC'),
    (lambda p:p.update(temperature='HOT'),'PRODUCT_SPEC'),
    (lambda p:p.pop('group'),'PARTNER_UNVERIFIED'),
])
def test_target_terms_block_before_merchant_side_effects(tmp_path,change,reason):
    st=JourneyStore(tmp_path/'orders.sqlite');s=order(st)
    s=alter(st,s,'oat',lambda shop:change(shop['transfer_policy']))
    before=deepcopy(s);p=plan(s,'oat')
    assert reason in p['reasons'] and not p['feasible']
    with pytest.raises(Conflict): act(st,s,'transfer',quote_id=p['quote_id'])
    after=st.get(s['id'])
    assert after['order']==before['order'] and after['wallet']==before['wallet']
    assert after['merchant_capacity']==before['merchant_capacity']
    assert after['events']==before['events']


def test_source_participation_is_required_too(tmp_path):
    st=JourneyStore(tmp_path/'orders.sqlite');s=order(st)
    s=alter(st,s,'wave',lambda shop:shop['transfer_policy'].update(enabled=False))
    assert 'PARTNER_DISABLED' in plan(s,'oat')['reasons']
    assert not s['recommendations']


def test_changed_terms_invalidate_quote_even_without_version_change(tmp_path):
    st=JourneyStore(tmp_path/'orders.sqlite');s=order(st);old=plan(s,'oat')['quote_id']
    s=alter(st,s,'oat',lambda shop:shop['transfer_policy'].update(revision=2))
    assert old!=plan(s,'oat')['quote_id']
    with pytest.raises(Conflict) as e: act(st,s,'transfer',quote_id=old)
    assert e.value.code=='STALE_QUOTE'


def test_nonparticipating_shop_still_accepts_its_own_initial_order(tmp_path):
    st=JourneyStore(tmp_path/'orders.sqlite')
    s=st.create(Intent(budget=8000,deadline_minutes=35,max_detour=10).model_dump())
    assert plan(s,'garden')['feasible']
    s=act(st,s,'reserve',quote_id=plan(s,'garden')['quote_id'])
    assert s['order']['store_id']=='garden'
    assert not any(p['feasible'] for p in s['all_plans'] if p['store_id']!='garden')


def test_accepted_terms_and_one_settlement_are_preserved_on_retries(tmp_path):
    st=JourneyStore(tmp_path/'orders.sqlite');s=order(st);p=plan(s,'oat');terms=deepcopy(p['transfer_terms'])
    s=act(st,s,'transfer',quote_id=p['quote_id'])
    assert s['order']['commercial_terms']==terms
    transfer=next(e for e in s['events'] if e['type']=='ORDER_TRANSFERRED')
    assert transfer['data']['accepted_terms']['terms_id']==terms['terms_id']
    s=act(st,s,'advance',minutes=max(0,s['current_plan']['start_at']-s['clock']))
    s=act(st,s,'start');s=act(st,s,'advance',minutes=s['order']['ready_at']-s['clock']);s=act(st,s,'ready')
    code=s['order']['pickup_code'];s=act(st,s,'claim',pickup_code=code);s=act(st,s,'claim',pickup_code=code)
    entries=[e for e in s['events'] if e['type']=='MERCHANT_SETTLEMENT_SIMULATED']
    assert len(entries)==1 and entries[0]['data']['merchant_store_id']=='oat'
    assert entries[0]['data']['merchant_receivable']==4700
    assert s['receipt']['settlement']==entries[0]['data']
    assert s['receipt']['capture_count']==1 and s['wallet']['earned']==32


def test_store_funded_coupon_does_not_charge_platform_twice(tmp_path):
    from demo.route.agreement import funding
    p=dict(gross=4300,coupon_id='wave1000',coupon_discount=1000,points_used=1000,cash_due=2300)
    m=funding('wave',p)
    assert m['merchant_coupon']==1000 and m['platform_coupon']==0
    assert m['merchant_receivable']==3300==m['customer_cash']+m['platform_points']


def test_invalid_funding_is_rejected_not_presented_as_balanced():
    from demo.route.agreement import funding
    with pytest.raises(ValueError): funding('wave',dict(gross=4300,coupon_id=None,coupon_discount=0,points_used=1000,cash_due=3301))


def test_original_product_spec_is_the_accepted_one_not_mutable_catalogue(tmp_path):
    st=JourneyStore(tmp_path/'orders.sqlite');s=order(st)
    s=alter(st,s,'wave',lambda shop:shop['transfer_policy'].update(volume_ml=240))
    s=alter(st,s,'oat',lambda shop:shop['transfer_policy'].update(volume_ml=240))
    p=plan(s,'oat')
    assert not p['feasible'] and 'PRODUCT_SPEC' in p['reasons']
    assert p['transfer_terms']['source_product']['volume_ml']==360


@pytest.mark.parametrize('coupon,discount',[(None,500),('unknown',500)])
def test_undocumented_coupon_funder_is_not_assumed(coupon,discount):
    from demo.route.agreement import funding
    with pytest.raises(ValueError):funding('wave',dict(gross=4300,coupon_id=coupon,coupon_discount=discount,points_used=1000,cash_due=2800))
