"""Kill the real coordinator process after independent commits, not just objects."""
import json
import subprocess
import sys
import pytest
from demo.route.store import JourneyStore
from test_payment_operations import system, fresh, ordered, ready, auto, ledger, command
from test_transfer_durability import plan

CHILD = r'''
import json, os, sys
from demo.route.store import JourneyStore
from demo.route.merchant_http import HttpMerchantFleet
from demo.route.payments.http_client import PaymentClient
from demo.route.payment_operations import PaymentOperations
v=json.loads(sys.stdin.read())
fleet=HttpMerchantFleet(v['merchant_url'],v['merchant_token'])
pg=PaymentClient(v['payment_url'],v['payment_token'])
st=JourneyStore(v['db'],fleet=fleet,payment_gateway=pg)
original=PaymentOperations._external
def interrupt(self, op):
    result=original(self,op)
    if op['phase']==v['stop_after'] and result.get('ok'):
        os._exit(91)
    return result
PaymentOperations._external=interrupt
st.command(v['sid'],v['command'])
raise AssertionError('expected crash boundary was not exercised')
'''


@pytest.mark.parametrize('stop_after',['PAY_AUTHORIZE','RELEASE_SOURCE'])
def test_coordinator_process_death_reuses_committed_external_results(system,stop_after):
    st,pg=system
    s=fresh(st) if stop_after=='PAY_AUTHORIZE' else ordered(st)
    target='wave' if stop_after=='PAY_AUTHORIZE' else 'oat'
    data=dict(db=st.path,sid=s['id'],merchant_url=st.fleet.url,merchant_token=st.fleet.token,
        payment_url=pg.client.url,payment_token=pg.client.token,stop_after=stop_after,
        command=dict(action='reserve' if stop_after=='PAY_AUTHORIZE' else 'transfer',
                     quote_id=plan(s,target)['quote_id'],expected_version=s['version'],request_id='crash-boundary'))
    # Private loopback tokens go through stdin, never the command line or a log.
    child=subprocess.run([sys.executable,'-c',CHILD],input=json.dumps(data),text=True,
                         stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=15)
    assert child.returncode==91,child.stderr
    pending=st.get(s['id'])
    assert pending['handoff_pending'] and pending['handoff']['phase']==stop_after
    oid=pending['pending_order_id']
    bank=ledger(st,pg,pending)
    if stop_after=='PAY_AUTHORIZE':
        assert pending['order'] is None and len(bank['authorizations'])==1
    else:
        assert pending['handoff']['decision']=='COMMIT'
        assert pending['order']['id']==oid and pending['order']['store_id']=='wave'
        assert pending['merchant_capacity']['wave']['used']==0
    reopened=JourneyStore(st.path,fleet=st.fleet,payment_gateway=pg.client)
    saved=auto(reopened,pending)
    assert saved['order']['id']==oid and saved['order']['store_id']==target
    bank=ledger(st,pg,saved)
    assert len(bank['authorizations'])==(1 if stop_after=='PAY_AUTHORIZE' else 2)
    assert bank['held_krw']==saved['order']['price'] and bank['capture_count']==0
    done=ready(reopened,saved)
    done=command(reopened,done,'claim',pickup_code=done['order']['pickup_code'])
    assert done['order']['id']==oid and done['order']['state']=='PICKED_UP'
    assert ledger(reopened,pg,done)['capture_count']==1


def test_merchant_claim_reply_loss_after_capture_keeps_one_charge_and_one_claim(system):
    st,pg=system;s=ready(st,ordered(st));fleet=st.fleet
    class ClaimReplyLoss:
        handles_response_loss=True
        def execute(self,shop,**kwargs):
            if kwargs['action']=='CLAIM':kwargs['lose_reply']=True
            return fleet.execute(shop,**kwargs)
        def snapshot(self,*args):return fleet.snapshot(*args)
        def evidence(self,*args):return fleet.evidence(*args)
    st.fleet=ClaimReplyLoss()
    s=command(st,s,'claim',pickup_code=s['order']['pickup_code'])
    assert s['handoff_pending'] and s['handoff']['phase']=='CLAIM'
    assert s['order']['state']=='READY' and s['wallet']['spent']==0
    assert ledger(st,pg,s)['capture_count']==1
    merchant=fleet.evidence(s['world_id'],s['order']['id'])
    assert merchant['merchants']['wave']['reservation']['phase']=='CLAIMED'
    saved=auto(st,s)
    assert saved['order']['state']=='PICKED_UP' and saved['wallet']['spent']==1000
    assert ledger(st,pg,saved)['capture_count']==1
    assert sum(e['type']=='BENEFITS_CONSUMED' for e in saved['events'])==1
