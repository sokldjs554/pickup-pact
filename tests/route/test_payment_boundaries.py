"""Less common monetary paths and delayed replies across coordinator restarts."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import threading
import time
import pytest
from test_payment_operations import system, fresh, ordered, ready, auto, fault, command, plan, ledger
from demo.route.store import JourneyStore
from demo.route.recovery_worker import RecoveryWorker


def set_prices(st,s,changes):
    # Fixture data in this visitor's persisted world; production has no price-write endpoint.
    with st.connection() as db:
        raw=st._load(db,s['id'])
        for shop in raw['stores']:
            if shop['id'] in changes:shop['prices']['latte']=changes[shop['id']]
        st._save(db,raw)
    return st.get(s['id'])


def test_equal_price_transfer_reuses_exact_authorization(system):
    st,pg=system;s=fresh(st);s=set_prices(st,s,{'oat':4300})
    s=command(st,s,'reserve',quote_id=plan(s,'wave')['quote_id']);old=s['payment']['authorization']
    s=command(st,s,'transfer',quote_id=plan(s,'oat')['quote_id'])
    assert s['order']['price']==2800 and s['payment']['authorization']==old
    assert len(ledger(st,pg,s)['transactions'])==1


def test_lower_price_transfer_uses_new_approval_and_releases_old(system):
    st,pg=system;s=fresh(st);s=set_prices(st,s,{'oat':3900})
    s=command(st,s,'reserve',quote_id=plan(s,'wave')['quote_id'])
    s=command(st,s,'transfer',quote_id=plan(s,'oat')['quote_id'])
    bank=ledger(st,pg,s)
    assert s['order']['price']==2400 and bank['held_krw']==2400
    assert sorted(a['status'] for a in bank['authorizations'])==['AUTHORIZED','VOIDED']


def test_zero_cash_order_uses_no_approval_or_capture(system):
    st,pg=system
    intent={'destination':'office','deadline_minutes':16,'drink':'latte','milk':'regular','decaf':False,'budget':5500,'max_detour':5,'priority':'arrival','coupon_id':None,'points':2000}
    s=st.create(intent);s=set_prices(st,s,{'wave':1800})
    s=command(st,s,'reserve',quote_id=plan(s,'wave')['quote_id'])
    assert s['payment']['state']=='NO_CHARGE' and s['order']['price']==0
    s=ready(st,s);s=command(st,s,'claim',pickup_code=s['order']['pickup_code'])
    assert ledger(st,pg,s)['transactions']==[] and s['receipt']['capture_count']==0
    assert s['wallet']['spent']==1800 and s['wallet']['earned']==0
    from demo.route.reconciliation import reconcile
    assert reconcile(st,s['id'])['status']=='MATCH'


def test_slow_approval_receipt_after_abort_cannot_restore_target(system):
    st,pg=system;s=ordered(st);old=s['order']['id'];gateway=st.payment_gateway
    entered=threading.Event();release=threading.Event();block=True
    class DelayOnce:
        def operation(self,c):return gateway.operation(c)
        def snapshot(self,*a):return gateway.snapshot(*a)
        def execute(self,c,**kw):
            nonlocal block
            result=gateway.execute(c,**kw)
            if c['action']=='AUTHORIZE' and block:
                block=False;entered.set();assert release.wait(5)
            return result
    st.payment_gateway=DelayOnce()
    with ThreadPoolExecutor(max_workers=2) as pool:
        future=pool.submit(command,st,s,'transfer',quote_id=plan(s,'oat')['quote_id'])
        assert entered.wait(3)
        pending=st.get(s['id']);assert pending['handoff_pending']
        reopened=JourneyStore(st.path,fleet=st.fleet,payment_gateway=gateway)
        try:
            RecoveryWorker(reopened).run_once(now=pending['handoff']['recovery']['deadline_at']+1)
            done=auto(reopened,reopened.get(s['id']))
            assert done['handoff']['decision']=='ABORT' and done['order']['store_id']=='wave'
        finally:release.set()
        future.result(timeout=5)
    done=st.get(s['id']);assert done['order']['id']==old and done['order']['store_id']=='wave'
    assert ledger(st,pg,done)['held_krw']==2800
    assert done['merchant_capacity']['oat']['used']==0
