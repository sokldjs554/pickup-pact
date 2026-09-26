"""A competent reserve-first baseline must be allowed to tie our protection."""
from copy import deepcopy
from uuid import uuid4
import pytest
from demo.route.api import Intent
from demo.route.store import JourneyStore, Conflict
from demo.route import benefits
from demo.route.planner import plans


def act(st,s,action,**extra):
    return st.command(s['id'],dict(action=action,expected_version=s['version'],request_id=uuid4().hex,**extra))


def ordered(st):
    s=st.create(Intent(coupon_id='welcome500',points=1000).model_dump())
    q=next(p for p in s['all_plans'] if p['store_id']=='wave')
    return act(st,s,'reserve',quote_id=q['quote_id'])


def request(s):
    q=next(p for p in s['all_plans'] if p['store_id']=='oat')
    return dict(action='reserve_first_reorder',expected_version=s['version'],request_id=uuid4().hex,
                quote_id=q['quote_id'],accepted_cash_due=3200)


def test_reserve_first_reorder_keeps_protection_but_issues_new_order(tmp_path):
    st=JourneyStore(tmp_path/'orders.sqlite');s=ordered(st);oid=s['order']['id'];cmd=request(s)
    done=st.command(s['id'],cmd)
    assert done['handoff']['status']=='COMPLETED'
    assert done['order']['id']!=oid and done['order']['store_id']=='oat'
    assert done['receipt']['authorization_count']==2 and done['receipt']['capture_count']==0
    assert done['wallet']['held_points']==1000 and done['order']['price']==3200
    assert done['merchant_capacity']['wave']['used']==0 and done['merchant_capacity']['oat']['used']==1
    assert st.command(s['id'],cmd)['order']==done['order']


@pytest.mark.parametrize('failure',['reject','full'])
def test_reserve_first_reorder_preserves_original_on_target_failure(tmp_path,failure):
    st=JourneyStore(tmp_path/'orders.sqlite');s=ordered(st);before=deepcopy(s)
    if failure=='reject': st.fleet.set_accepting('oat',s['world_id'],False)
    else: st.fleet.execute('oat',world=s['world_id'],order_id='OTHER',generation=0,operation_id='other',action='ADMIT')
    done=st.command(s['id'],request(s))
    assert done['handoff']['status']=='REJECTED'
    assert done['order']==before['order'] and done['wallet']==before['wallet']
    assert done['events']==before['events']
    assert done['merchant_capacity']['wave']['used']==1


def crash(path,sid,cmd,phase):
    import os
    st=JourneyStore(path)
    def stop(shop,action,key,result):
        if action==phase and result['ok']: os._exit(93)
    st.fleet.after_commit=stop
    st.command(sid,cmd)


@pytest.mark.parametrize('phase',['FREEZE','HOLD','RELEASE_SOURCE','ACTIVATE'])
def test_reserve_first_reorder_survives_each_independent_commit(tmp_path,phase):
    import multiprocessing
    path=tmp_path/'orders.sqlite';st=JourneyStore(path);s=ordered(st);cmd=request(s)
    p=multiprocessing.get_context('spawn').Process(target=crash,args=(str(path),s['id'],cmd,phase))
    p.start();p.join(12)
    assert p.exitcode==93
    st=JourneyStore(path);done=st.command(s['id'],cmd)
    assert done['handoff']['status']=='COMPLETED' and done['order']['id']!=s['order']['id']
    assert sum(x['used'] for x in done['merchant_capacity'].values())==1
    assert done['receipt']['authorization_count']==2


def test_reserve_first_does_not_silently_accept_new_price(tmp_path):
    st=JourneyStore(tmp_path/'orders.sqlite');s=ordered(st);cmd=request(s);cmd['accepted_cash_due']=1
    with pytest.raises(Conflict):st.command(s['id'],cmd)
    assert st.get(s['id'])['order']==s['order']


def test_comparison_includes_stronger_baseline_and_honest_ties():
    from demo.route.transfer_comparison import run_transfer_comparison
    report=run_transfer_comparison(Intent(coupon_id='welcome500',points=1000).model_dump())
    assert report['policies']==['stay','cancel_reorder','reserve_first_reorder','guarded_transfer']
    for name in ['reject_after_quote','last_place_taken']:
        row=next(r for r in report['cases'] if r['scenario']==name)
        assert row['reserve_first_reorder']['original_preserved'] is True
        assert row['guarded_transfer']['original_preserved'] is True
        assert row['cancel_reorder']['has_order'] is False
    row=next(r for r in report['cases'] if r['scenario']=='normal')
    assert row['reserve_first_reorder']['has_order'] and not row['reserve_first_reorder']['same_order']
    assert row['reserve_first_reorder']['cash_due']==row['guarded_transfer']['cash_due']
    assert '같은' in report['baseline_design'] and '독립' in report['baseline_design']
