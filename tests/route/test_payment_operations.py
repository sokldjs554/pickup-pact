"""Order/merchant/PG consistency under actual independent HTTP effects."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import sqlite3
import threading
import time
from uuid import uuid4
import pytest
from demo.route.store import JourneyStore, Conflict
from demo.route.api import Intent
from demo.route.runtime import MerchantProcess
from demo.route.payments.runtime import PaymentProcess
from test_transfer_durability import command, plan, configure


@pytest.fixture
def system(tmp_path):
    with MerchantProcess(str(tmp_path/'merchants')) as fleet, PaymentProcess(tmp_path/'pg') as pg:
        st=JourneyStore(tmp_path/'orders.sqlite',fleet=fleet,payment_gateway=pg.client)
        yield st,pg


def fresh(st,**extra):
    return st.create(Intent(coupon_id='welcome500',points=1000).model_dump(),**extra)


def ordered(st,**extra):
    s=fresh(st,**extra);return command(st,s,'reserve',quote_id=plan(s,'wave')['quote_id'])


def fault(st,s,value):
    return st.payment_control(s['id'],dict(action='fault',fault=value,request_id=uuid4().hex,expected_version=s['version']))


def auto(st,s):
    from demo.route.recovery_worker import RecoveryWorker
    for _ in range(12):
        s=st.get(s['id'])
        if not s['handoff_pending']:return s
        assert s['handoff']['recovery']['state']!='REVIEW_REQUIRED',s['handoff']
        RecoveryWorker(st).run_once(now=s['handoff']['recovery']['next_retry_at']+.01)
    pytest.fail('operation did not converge')


def ready(st,s):
    delta=max(0,s['current_plan']['start_at']-s['clock'])
    if delta:s=command(st,s,'advance',minutes=delta)
    s=command(st,s,'start');s=command(st,s,'advance',minutes=s['order']['ready_at']-s['clock'])
    return command(st,s,'ready')


def ledger(st,pg,s):
    oid=s['order']['id'] if s.get('order') else s.get('pending_order_id') or s['payment_order_id']
    return pg.client.snapshot(s['world_id'],oid)


def test_initial_order_is_backed_by_real_pg_and_real_merchant(system):
    st,pg=system;s=ordered(st)
    assert s['protocol_version']==2 and s['payment_mode']=='simulator_http_v1'
    assert s['order']['state']=='RESERVED' and s['payment']['state']=='AUTHORIZED'
    assert ledger(st,pg,s)['held_krw']==2800
    assert s['merchant_capacity']['wave']['used']==1
    event=next(e for e in s['events'] if e['type']=='PAYMENT_AUTHORIZED')
    assert event['data']['transaction_id']==ledger(st,pg,s)['transactions'][0]['id']


def test_first_decline_does_not_reserve_a_seat_or_spend_points(system):
    st,pg=system;s=ordered(st,card_token='demo-declined')
    assert s['order'] is None and not s['handoff_pending']
    assert s['handoff']['failure']=='DECLINED'
    assert s['wallet']['held_points']==s['wallet']['spent']==0
    assert all(v['used']==0 for v in s['merchant_capacity'].values())
    assert ledger(st,pg,s)['transactions']==[]


def test_initial_reply_loss_has_pending_id_no_order_and_same_approval_after_recovery(system):
    st,pg=system;s=fresh(st,payment_fault='authorize_reply_lost')
    request=dict(action='reserve',quote_id=plan(s,'wave')['quote_id'],request_id='first',expected_version=s['version'])
    s=st.command(s['id'],request)
    assert s['order'] is None and s['handoff_pending'] and s['pending_order_id']
    assert s['wallet']['held_points']==0 and s['merchant_capacity']['wave']['used']==0
    oid=s['pending_order_id'];assert ledger(st,pg,s)['held_krw']==2800
    with pytest.raises(Conflict):command(st,s,'reserve',quote_id=plan(s,'wave')['quote_id'])
    s=auto(st,s);assert s['order']['id']==oid
    assert len(ledger(st,pg,s)['authorizations'])==1
    assert st.command(s['id'],request)['order']['id']==oid


def test_admission_rejected_after_approval_voids_that_approval(system):
    st,pg=system;s=fresh(st)
    st.fleet.set_accepting('wave',s['world_id'],False)
    s=command(st,s,'reserve',quote_id=plan(s,'wave')['quote_id'])
    assert s['order'] is None and s['handoff']['status']=='REJECTED'
    bank=ledger(st,pg,s);assert bank['held_krw']==bank['captured_krw']==0
    assert [t['kind'] for t in bank['transactions']]==['AUTHORIZE','VOID']


def test_replacement_2800_to_3200_releases_old_before_one_final_charge(system):
    st,pg=system;s=ordered(st);oid=s['order']['id'];old=s['payment']['authorization']['authorization_id']
    s=command(st,s,'transfer',quote_id=plan(s,'oat')['quote_id'])
    assert s['order']['id']==oid and s['order']['price']==3200
    bank=ledger(st,pg,s);assert bank['held_krw']==3200 and bank['captured_krw']==0
    assert next(a for a in bank['authorizations'] if a['authorization_id']==old)['status']=='VOIDED'
    s=ready(st,s);s=command(st,s,'claim',pickup_code=s['order']['pickup_code'])
    bank=ledger(st,pg,s)
    assert bank['held_krw']==0 and bank['captured_krw']==3200 and bank['capture_count']==1
    assert s['wallet']['spent']==1000 and s['wallet']['earned']==32
    assert s['receipt']['capture_count']==1 and s['receipt']['authorization_count']==2


def test_target_rejection_never_voids_old_approval(system):
    st,pg=system;s=ordered(st);before=deepcopy(s)
    s=configure(st,s,'target_reject');s=command(st,s,'transfer',quote_id=plan(s,'oat')['quote_id'])
    assert s['order']==before['order'] and s['wallet']==before['wallet']
    assert s['payment']['authorization']==before['payment']['authorization']
    assert len(ledger(st,pg,s)['transactions'])==1


def test_new_approval_limit_failure_preserves_old_order(system):
    st,pg=system;s=ordered(st);before=deepcopy(s)
    with sqlite3.connect(pg.directory/'payments.sqlite') as db:
        db.execute("UPDATE payment_settings SET value=5900 WHERE name='limit_krw'")
    # A new server reads the same persisted virtual limit, with the old authorization intact.
    pg.process.kill();pg.process.wait(3);time.sleep(.6)
    s=command(st,s,'transfer',quote_id=plan(s,'oat')['quote_id']);s=auto(st,s)
    assert s['handoff']['status']=='REJECTED' and s['handoff']['failure']=='LIMIT_EXCEEDED'
    assert s['order']==before['order'] and s['wallet']==before['wallet']
    assert ledger(st,pg,s)['held_krw']==2800
    assert s['merchant_capacity']['oat']['used']==0


@pytest.mark.parametrize('which',['capture_reply_lost','void_reply_lost'])
def test_final_payment_reply_loss_does_not_finalize_local_order_early(system,which):
    st,pg=system;s=ordered(st)
    if which=='capture_reply_lost':s=ready(st,s)
    s=fault(st,s,which)
    if which=='capture_reply_lost':s=command(st,s,'claim',pickup_code=s['order']['pickup_code'])
    else:s=command(st,s,'cancel')
    assert s['handoff_pending']
    assert s['order']['state'] not in {'CANCELLED','PICKED_UP'}
    if which=='capture_reply_lost':
        assert ledger(st,pg,s)['capture_count']==1 and s['wallet']['spent']==0
    else:assert ledger(st,pg,s)['held_krw']==0 and s['wallet']['held_points']==1000
    s=auto(st,s)
    assert s['order']['state']==('PICKED_UP' if which=='capture_reply_lost' else 'CANCELLED')
    assert ledger(st,pg,s)['capture_count']==(1 if which=='capture_reply_lost' else 0)


def test_reply_lost_during_replacement_expiry_releases_new_not_old(system):
    st,pg=system;s=ordered(st);before=deepcopy(s)
    s=fault(st,s,'authorize_reply_lost');s=command(st,s,'transfer',quote_id=plan(s,'oat')['quote_id'])
    assert s['handoff_pending'] and ledger(st,pg,s)['held_krw']==6000
    from demo.route.recovery_worker import RecoveryWorker
    RecoveryWorker(st).run_once(now=s['handoff']['recovery']['deadline_at']+1)
    s=auto(st,st.get(s['id']))
    assert s['handoff']['decision']=='ABORT' and s['handoff']['status']=='REJECTED'
    assert s['order']==before['order'] and s['wallet']==before['wallet']
    assert ledger(st,pg,s)['held_krw']==2800 and s['merchant_capacity']['oat']['used']==0


@pytest.mark.parametrize('merchant_fault',['after_target_hold','after_source_release','after_target_activation'])
def test_merchant_reply_loss_uses_saved_payment_and_does_not_revive_source(system,merchant_fault):
    st,pg=system;s=ordered(st);oid=s['order']['id']
    s=configure(st,s,merchant_fault);s=command(st,s,'transfer',quote_id=plan(s,'oat')['quote_id'])
    assert s['handoff_pending']
    reopened=JourneyStore(st.path,fleet=st.fleet,payment_gateway=pg.client)
    s=auto(reopened,s)
    assert s['order']['id']==oid and s['order']['store_id']=='oat'
    assert s['merchant_capacity']['wave']['used']==0
    assert len(ledger(st,pg,s)['authorizations'])==2 and ledger(st,pg,s)['held_krw']==3200


def test_twenty_four_order_claims_only_one_payment_pickup_and_earning(system):
    st,pg=system;s=ready(st,ordered(st));code=s['order']['pickup_code']
    def claim(_):
        current=st.get(s['id'])
        try:return command(st,current,'claim',pickup_code=code)
        except Conflict:return None
    with ThreadPoolExecutor(max_workers=12) as pool:list(pool.map(claim,range(24)))
    s=auto(st,st.get(s['id']))
    assert s['order']['state']=='PICKED_UP' and s['wallet']['spent']==1000 and s['wallet']['earned']==28
    assert ledger(st,pg,s)['capture_count']==1
    assert sum(e['type']=='BENEFITS_CONSUMED' for e in s['events'])==1


def test_payment_io_does_not_hold_global_order_write_lock(system):
    st,pg=system;s=fresh(st);entered=threading.Event();release=threading.Event()
    gateway=st.payment_gateway
    class Slow:
        transport='http'
        def operation(self,c):
            entered.set();assert release.wait(3)
            return gateway.operation(c)
        def execute(self,*a,**kw):return gateway.execute(*a,**kw)
        def snapshot(self,*a):return gateway.snapshot(*a)
    st.payment_gateway=Slow()
    with ThreadPoolExecutor(max_workers=2) as pool:
        f=pool.submit(command,st,s,'reserve',quote_id=plan(s,'wave')['quote_id'])
        assert entered.wait(2)
        g=pool.submit(fresh,st)
        try:
            other=g.result(timeout=1);assert other['id']!=s['id']
        finally:release.set()
        assert f.result(timeout=4)['order']['state']=='RESERVED'


def test_unavailable_pg_exhaustion_never_falls_back_or_resets_on_get(system):
    st,pg=system;s=fresh(st)
    class Down:
        transport='http'
        def operation(self,c):raise OSError('network unavailable')
    st.payment_gateway=Down()
    s=command(st,s,'reserve',quote_id=plan(s,'wave')['quote_id'])
    from demo.route.recovery_worker import RecoveryWorker
    for _ in range(8):
        if s['handoff']['recovery']['state']=='REVIEW_REQUIRED':break
        RecoveryWorker(st).run_once(now=s['handoff']['recovery']['next_retry_at']+.01);s=st.get(s['id'])
    assert s['handoff']['recovery']['state']=='REVIEW_REQUIRED' and s['order'] is None
    assert s['handoff']['recovery']['retry_count']==8
    for _ in range(3):assert st.get(s['id'])['handoff']['recovery']['retry_count']==8
    assert not any(e['type']=='PAYMENT_AUTHORIZED' for e in s['events'])


def test_existing_legacy_journey_does_not_silently_convert(system,tmp_path):
    st,pg=system
    legacy=JourneyStore(tmp_path/'legacy.sqlite');s=fresh(legacy)
    upgraded=JourneyStore(legacy.path,fleet=legacy.fleet,payment_gateway=pg.client)
    s=command(upgraded,s,'reserve',quote_id=plan(s,'wave')['quote_id'])
    assert s.get('protocol_version',1)==1
    assert pg.client.snapshot(s['world_id'],s['order']['id'])['transactions']==[]


def test_capture_fault_survives_nonfinancial_manufacturing_steps(system):
    st,pg=system;s=ordered(st);s=fault(st,s,'capture_reply_lost')
    s=ready(st,s)
    assert s['next_payment_fault']=='capture_reply_lost'
    s=command(st,s,'claim',pickup_code=s['order']['pickup_code'])
    assert s['handoff_pending'] and ledger(st,pg,s)['capture_count']==1
    assert auto(st,s)['order']['state']=='PICKED_UP'
