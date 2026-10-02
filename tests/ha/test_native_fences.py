"""Integrated safety boundaries use actual shared storage, not queue-only mocks."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
import time
from uuid import uuid4
import pytest
from test_native_journey import integrated, fresh, quote, cmd, picked
from demo.route.reconciliation import reconcile
from demo.route.store import Conflict


def test_late_owner_cannot_overwrite_takeover_and_does_not_hold_order_lock(integrated):
    stores, _, pg=integrated
    a,b=stores
    a.operations.lease_seconds=b.operations.lease_seconds=1
    client=a.payment_gateway
    entered,release=threading.Event(),threading.Event()
    class Delayed:
        def operation(self,c):return client.operation(c)
        def snapshot(self,*args):return client.snapshot(*args)
        def execute(self,c,**kw):
            result=client.execute(c,**kw)
            entered.set()
            assert release.wait(15)
            return result
    a.payment_gateway=Delayed()
    s=fresh(a)
    with ThreadPoolExecutor(max_workers=2) as pool:
        future=pool.submit(cmd,a,s,'reserve',quote_id=quote(s,'wave'))
        try:
            assert entered.wait(10)
            pending=b.get(s['id'])
            assert pending['handoff_pending'] and pending['order'] is None
            other=fresh(b)
            assert cmd(b,other,'reserve',quote_id=quote(other,'wave'))['order']
            time.sleep(1.1)
            restored=b.operations.resume(s['id'],pending['handoff']['id'])
            assert restored['order'] and not restored['handoff_pending']
            version=restored['version'];oid=restored['order']['id']
        finally:release.set()
        late=future.result(10)
    assert late['version']==version and late['order']['id']==oid
    assert pg.snapshot(s['world_id'],oid)['held_krw']==2800
    final=picked(b,late)
    assert reconcile(b,s['id'])['status']=='MATCH' and final['receipt']['capture_count']==1


def test_review_required_is_readable_but_never_automatically_claimed(integrated):
    stores,_,_=integrated
    a,b=stores
    client=a.payment_gateway
    class Missing:
        def operation(self,c):raise OSError('unavailable')
    a.payment_gateway=Missing()
    s=fresh(a)
    s=cmd(a,s,'reserve',quote_id=quote(s,'wave'))
    for _ in range(7):s=a.operations.resume(s['id'],s['handoff']['id'])
    assert s['handoff']['recovery']['state']=='REVIEW_REQUIRED'
    assert not a.repository.due()
    a.payment_gateway=client
    before=b.get(s['id'])
    assert b.operations.resume(s['id'],s['handoff']['id'],automatic=True)==before
    after=cmd(b,before,'recover')
    assert after['order'] and not after['handoff_pending']


def test_payment_notification_can_wake_but_never_apply_order_money(integrated):
    stores,_,pg=integrated
    a,b=stores
    s=fresh(a,payment_fault='authorize_reply_lost')
    s=cmd(a,s,'reserve',quote_id=quote(s,'wave'))
    before=b.read_state(s['id'])
    claim=pg.claim_notifications('notif',limit=1)[0]
    body=claim['body'].encode()
    from demo.route.payments.notifications import sign
    stamp=str(int(time.time()));signature=sign(a.payment_inbox.secret,stamp,body)
    assert a.payment_inbox.accept(body,stamp,signature)=={'accepted':True,'duplicate':False}
    assert b.payment_inbox.accept(body,stamp,signature)=={'accepted':True,'duplicate':True}
    assert b.read_state(s['id'])==before
    forged=json.dumps(json.loads(body)|{'amount_krw':1}).encode()
    from demo.route.payments.domain import PaymentError
    with pytest.raises(PaymentError):b.payment_inbox.accept(forged,stamp,sign(a.payment_inbox.secret,stamp,forged))
    result=b.operations.resume(s['id'],s['handoff']['id'])
    assert result['order'] and pg.snapshot(s['world_id'],result['order']['id'])['capture_count']==0


def test_capacity_guest_control_replays_across_apps(integrated):
    stores,fleets,_=integrated
    a,b=stores
    s=fresh(a)
    control=dict(action='occupy',store_id='oat',request_id='guest',expected_version=s['version'])
    s=a.transfer_control(s['id'],control)
    again=b.transfer_control(s['id'],control)
    assert again['duplicate']
    assert fleets[0].snapshot(s['world_id'])['oat']['used']==1
    cleared=b.transfer_control(s['id'],dict(action='clear',store_id='oat',request_id='clear',expected_version=again['version']))
    assert fleets[1].snapshot(s['world_id'])['oat']['used']==0 and not cleared['handoff_pending']
