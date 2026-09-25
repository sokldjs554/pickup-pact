"""New transfer guarantees exercised through real stores; no mocked pricing."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import uuid4
import pytest
from demo.route.store import JourneyStore, Conflict
from demo.route.api import Intent


def command(store, s, action, **extra):
    return store.command(s['id'], dict(action=action, expected_version=s['version'],request_id=uuid4().hex,**extra))


def plan(s, shop):
    return next(p for p in s['all_plans'] if p['store_id']==shop)


def ordered(store, world=None, shop='wave', **intent):
    args={} if world is None else {'world_id':world}
    s=store.create(Intent(coupon_id='welcome500',points=1000,**intent).model_dump(), **args)
    return command(store,s,'reserve',quote_id=plan(s,shop)['quote_id'])


def configure(store,s,fault):
    return store.transfer_control(s['id'],dict(action='fault',fault=fault,expected_version=s['version'],request_id=uuid4().hex))


def test_new_journey_has_real_shared_merchant_admission(tmp_path):
    st=JourneyStore(tmp_path/'orders.sqlite');s=ordered(st)
    assert 'merchant_capacity' in s
    assert s['merchant_capacity']['wave']['used']==1
    assert s['merchant_capacity']['oat']['available']==1


def test_target_rejection_keeps_exact_original_order_and_benefits(tmp_path):
    st=JourneyStore(tmp_path/'orders.sqlite');s=ordered(st)
    s=configure(st,s,'target_reject'); before=deepcopy(s)
    s=command(st,s,'transfer',quote_id=plan(s,'oat')['quote_id'])
    assert s['handoff']['status']=='REJECTED'
    assert s['order']==before['order'] and s['wallet']==before['wallet']
    assert s['merchant_capacity']['wave']['used']==1 and s['merchant_capacity']['oat']['used']==0
    assert s['receipt']['capture_count']==0 and s['receipt']['transfers']==[]


@pytest.mark.parametrize('fault',['after_target_hold','after_source_release','after_target_activation'])
def test_uncertain_reply_is_not_success_and_reopen_recovers_once(tmp_path,fault):
    path=tmp_path/'orders.sqlite';st=JourneyStore(path);s=ordered(st)
    original=s['order']['id'];s=configure(st,s,fault)
    c=dict(action='transfer',expected_version=s['version'],request_id='lost-reply',quote_id=plan(s,'oat')['quote_id'])
    pending=st.command(s['id'],c)
    assert pending['handoff']['status']=='PENDING'
    with pytest.raises(Conflict): command(st,pending,'start')
    st=JourneyStore(path)
    done=st.command(s['id'],c)
    assert done['handoff']['status']=='COMPLETED'
    assert done['order']['id']==original and done['order']['store_id']=='oat'
    assert done['receipt']['authorization_count']==1
    assert len(done['receipt']['transfers'])==1 and done['wallet']['held_points']==1000
    assert done['merchant_capacity']['wave']['used']==0 and done['merchant_capacity']['oat']['used']==1
    assert st.command(s['id'],c)['receipt']==done['receipt']


def test_last_shared_place_has_one_winner_and_loser_keeps_order(tmp_path):
    st=JourneyStore(tmp_path/'orders.sqlite');a=ordered(st)
    b=ordered(st,world=a['world_id'])
    with ThreadPoolExecutor(max_workers=2) as ex:
        fs=[ex.submit(command,st,s,'transfer',quote_id=plan(s,'oat')['quote_id']) for s in (a,b)]
        rows=[f.result() for f in fs]
    assert sorted(s['handoff']['status'] for s in rows)==['COMPLETED','REJECTED']
    winner=next(s for s in rows if s['handoff']['status']=='COMPLETED')
    loser=next(s for s in rows if s['handoff']['status']=='REJECTED')
    assert winner['order']['store_id']=='oat' and loser['order']['store_id']=='wave'
    assert st.get(a['id'])['merchant_capacity']['oat']['used']==1
    assert winner['wallet']['held_points']==loser['wallet']['held_points']==1000


def test_cancel_and_reorder_use_new_id_but_same_wallet(tmp_path):
    st=JourneyStore(tmp_path/'orders.sqlite');s=ordered(st);oid=s['order']['id']
    s=command(st,s,'cancel');assert s['wallet']['available_points']==2000
    s=command(st,s,'reorder',quote_id=plan(s,'oat')['quote_id'])
    assert s['order']['id']!=oid and s['order']['store_id']=='oat'
    assert s['wallet']['held_points']==1000 and s['receipt']['authorization_count']==2


def test_rejected_transfer_can_later_retry_with_a_new_operation(tmp_path):
    st=JourneyStore(tmp_path/'orders.sqlite');s=ordered(st)
    s=configure(st,s,'target_reject')
    s=command(st,s,'transfer',quote_id=plan(s,'oat')['quote_id'])
    assert s['handoff']['status']=='REJECTED'
    s=command(st,s,'transfer',quote_id=plan(s,'oat')['quote_id'])
    assert s['handoff']['status']=='COMPLETED'
    assert s['order']['store_id']=='oat'


def _crash_worker(path, sid, request, phase):
    import os
    st=JourneyStore(path)
    def kill(shop, action, key, result):
        if action==phase and result['ok']:
            os._exit(91)
    st.fleet.after_commit=kill
    st.command(sid,request)


@pytest.mark.parametrize('phase',['FREEZE','HOLD','RELEASE_SOURCE','ACTIVATE'])
def test_process_death_after_merchant_commit_recovers_same_order(tmp_path,phase):
    import multiprocessing
    path=tmp_path/'orders.sqlite';st=JourneyStore(path);s=ordered(st)
    oid=s['order']['id'];request=dict(action='transfer',expected_version=s['version'],request_id='crash',quote_id=plan(s,'oat')['quote_id'])
    child=multiprocessing.get_context('spawn').Process(target=_crash_worker,args=(str(path),s['id'],request,phase))
    child.start();child.join(12)
    if child.is_alive():
        child.kill();child.join();pytest.fail('child did not reach requested committed boundary')
    assert child.exitcode==91
    recovered=JourneyStore(path)
    pending=recovered.get(s['id'])
    assert pending['handoff']['status']=='PENDING'
    all_seats=[seat for v in pending['merchant_capacity'].values() for seat in v['reservations'] if seat['order_id']==oid]
    assert len([v for v in all_seats if v['phase'] in {'RESERVED','PREPARING','READY'}])<=1
    finished=recovered.command(s['id'],request)
    assert finished['handoff']['status']=='COMPLETED'
    assert finished['order']['id']==oid and finished['order']['store_id']=='oat'
    assert len(finished['receipt']['transfers'])==1 and finished['wallet']['held_points']==1000


def test_old_merchant_generation_cannot_start_after_transfer(tmp_path):
    st=JourneyStore(tmp_path/'orders.sqlite');s=ordered(st)
    oid=s['order']['id'];s=command(st,s,'transfer',quote_id=plan(s,'oat')['quote_id'])
    r=st.fleet.execute('wave',world=s['world_id'],order_id=oid,generation=0,operation_id='stale-start',action='START')
    assert not r['ok']
    s=command(st,s,'transfer',quote_id=plan(s,'wave')['quote_id'])
    assert s['handoff']['status']=='COMPLETED'
    r=st.fleet.execute('wave',world=s['world_id'],order_id=oid,generation=0,operation_id='old-start-again',action='START')
    assert not r['ok'] and r['code']=='STALE_GENERATION'


def test_pending_transfer_forbids_money_clock_and_competing_commands(tmp_path):
    st=JourneyStore(tmp_path/'orders.sqlite');s=ordered(st);s=configure(st,s,'after_target_hold')
    s=command(st,s,'transfer',quote_id=plan(s,'oat')['quote_id'])
    before=deepcopy(s)
    for action,extra in [('cancel',{}),('advance',{'minutes':3}),('claim',{'pickup_code':'123456'}),('disrupt',{'store_id':'wave','minutes':12})]:
        with pytest.raises(Conflict) as err: command(st,s,action,**extra)
        assert err.value.code=='TRANSFER_PENDING'
    assert st.get(s['id'])==before


def test_concurrent_recovery_applies_transfer_once(tmp_path):
    st=JourneyStore(tmp_path/'orders.sqlite');s=ordered(st);s=configure(st,s,'after_target_hold')
    request=dict(action='transfer',expected_version=s['version'],request_id='concurrent',quote_id=plan(s,'oat')['quote_id'])
    s=st.command(s['id'],request)
    with ThreadPoolExecutor(max_workers=8) as ex:
        rows=list(ex.map(lambda _: JourneyStore(st.path).command(s['id'],request),range(24)))
    assert all(r['handoff']['status']=='COMPLETED' for r in rows)
    s=st.get(s['id'])
    assert s['receipt']['authorization_count']==1 and len(s['receipt']['transfers'])==1
    assert s['merchant_capacity']['oat']['used']==1


def test_competitor_changes_real_shared_admission_and_can_leave(tmp_path):
    st=JourneyStore(tmp_path/'orders.sqlite');s=ordered(st)
    control=dict(action='occupy',store_id='oat',expected_version=s['version'],request_id='one-guest')
    s=st.transfer_control(s['id'],control)
    st.transfer_control(s['id'],control)
    assert s['merchant_capacity']['oat']['used']==1
    s=command(st,s,'transfer',quote_id=plan(s,'oat')['quote_id'])
    assert s['handoff']['status']=='REJECTED' and s['order']['store_id']=='wave'
    s=st.transfer_control(s['id'],dict(action='clear',store_id='oat',expected_version=s['version'],request_id='guest-leaves'))
    s=command(st,s,'transfer',quote_id=plan(s,'oat')['quote_id'])
    assert s['handoff']['status']=='COMPLETED'


@pytest.mark.parametrize('phase',['ABORT_TARGET','UNFREEZE'])
def test_process_death_during_compensation_restores_original_once(tmp_path,phase):
    import multiprocessing
    path=tmp_path/'orders.sqlite';st=JourneyStore(path);s=ordered(st)
    s=configure(st,s,'target_reject');original=deepcopy(s)
    request=dict(action='transfer',expected_version=s['version'],request_id='abort-crash',quote_id=plan(s,'oat')['quote_id'])
    child=multiprocessing.get_context('spawn').Process(target=_crash_worker,args=(str(path),s['id'],request,phase))
    child.start();child.join(12)
    if child.is_alive():child.kill();child.join();pytest.fail('compensation boundary not reached')
    assert child.exitcode==91
    resumed=JourneyStore(path).command(s['id'],request)
    assert resumed['handoff']['status']=='REJECTED'
    assert resumed['order']==original['order'] and resumed['wallet']==original['wallet']
    assert resumed['merchant_capacity']['wave']['used']==1
    assert resumed['merchant_capacity']['oat']['used']==0
    assert resumed['receipt']['capture_count']==0


@pytest.mark.parametrize('phase',['ADMIT','START','CLAIM','CANCEL'])
def test_nontransfer_crash_does_not_duplicate_benefits_or_order(tmp_path,phase):
    import multiprocessing
    path=tmp_path/'orders.sqlite';st=JourneyStore(path)
    if phase=='ADMIT':
        s=st.create(Intent(coupon_id='welcome500',points=1000).model_dump())
        action,extra='reserve',dict(quote_id=plan(s,'wave')['quote_id'])
    else:
        s=ordered(st)
        if phase in {'START','CLAIM'}:
            s=command(st,s,'advance',minutes=max(0,s['current_plan']['start_at']-s['clock']))
        if phase=='CLAIM':
            s=command(st,s,'start')
            s=command(st,s,'advance',minutes=s['order']['ready_at']-s['clock'])
            s=command(st,s,'ready')
        action={'START':'start','CLAIM':'claim','CANCEL':'cancel'}[phase]
        extra=dict(pickup_code=s['order']['pickup_code']) if phase=='CLAIM' else {}
    request=dict(action=action,expected_version=s['version'],request_id='lifecycle-crash',**extra)
    child=multiprocessing.get_context('spawn').Process(target=_crash_worker,args=(str(path),s['id'],request,phase))
    child.start();child.join(12)
    if child.is_alive():child.kill();child.join();pytest.fail('lifecycle boundary not reached')
    assert child.exitcode==91
    fresh=JourneyStore(path);pending=fresh.get(s['id'])
    assert pending['handoff_pending']
    assert pending['wallet']==s['wallet']
    done=fresh.command(s['id'],request)
    assert done['handoff']['status']=='COMPLETED'
    assert done['receipt']['authorization_count']==1
    if phase=='CLAIM':
        assert done['receipt']['capture_count']==1
        assert done['wallet']['spent']==1000 and done['wallet']['earned']==28
    if phase=='CANCEL':assert done['wallet']['available_points']==2000
    again=fresh.command(s['id'],request)
    assert again['wallet']==done['wallet'] and again['receipt']==done['receipt']


def test_independent_coordinators_race_for_same_merchant_place(tmp_path):
    # Different coordinator transactions; only the target merchant DB serializes admission.
    from demo.route.merchant_fleet import MerchantFleet
    a=JourneyStore(tmp_path/'coordinator-a.sqlite');b=JourneyStore(tmp_path/'coordinator-b.sqlite')
    b.fleet=MerchantFleet(a.fleet.directory)
    left=ordered(a,world='shared');right=ordered(b,world='shared')
    with ThreadPoolExecutor(max_workers=2) as ex:
        tasks=[ex.submit(command,st,s,'transfer',quote_id=plan(s,'oat')['quote_id']) for st,s in [(a,left),(b,right)]]
        results=[f.result() for f in tasks]
    assert sorted(r['handoff']['status'] for r in results)==['COMPLETED','REJECTED']
    capacity=a.fleet.snapshot('shared')
    assert capacity['oat']['used']==1 and capacity['wave']['used']==1


def test_cancel_reorder_refusal_does_not_claim_original_order_survived(tmp_path):
    st=JourneyStore(tmp_path/'orders.sqlite');s=ordered(st)
    s=command(st,s,'cancel')
    st.fleet.set_accepting('oat',s['world_id'],False)
    s=command(st,s,'reorder',quote_id=plan(s,'oat')['quote_id'])
    assert s['handoff']['status']=='REJECTED'
    assert '원래 주문과 혜택은 그대로' not in s['handoff']['message']
    assert s['order']['state']=='CANCELLED'


def test_merchant_conflicting_idempotency_key_changes_nothing(tmp_path):
    st=JourneyStore(tmp_path/'orders.sqlite');s=ordered(st)
    kwargs=dict(world=s['world_id'],order_id='OTHER',generation=0,operation_id='same-key',action='ADMIT')
    first=st.fleet.execute('oat',**kwargs); assert first['ok']
    before=st.fleet.snapshot(s['world_id'])
    conflict=st.fleet.execute('oat',**{**kwargs,'action':'CANCEL'})
    assert conflict==dict(ok=False,code='COMMAND_CONFLICT')
    assert st.fleet.snapshot(s['world_id'])==before
