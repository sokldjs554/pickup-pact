"""Independent observations, not coordinator event counts dressed up as proof."""
from copy import deepcopy
import importlib
import json
import sqlite3
import pytest
from test_payment_operations import system, ordered, ready, auto, fresh, fault, command, plan


def check(st,s):
    try:module=importlib.import_module('demo.route.reconciliation')
    except ModuleNotFoundError:pytest.fail('Independent reconciliation is not implemented')
    return module.reconcile(st,s['id'])


def test_real_merchant_evidence_matches_seat_and_immutable_command(system):
    st,pg=system;s=ordered(st)
    assert hasattr(st.fleet,'evidence'),'merchant evidence API is missing'
    out=st.fleet.evidence(s['world_id'],s['order']['id'])
    shop=out['merchants']['wave']
    assert shop['reservation']['phase']=='RESERVED' and shop['revision']>=1
    assert shop['last_receipt']['action']=='ADMIT' and shop['last_receipt']['result']['ok']
    other=st.fleet.evidence('different-world',s['order']['id'])
    assert all(x['reservation'] is None and x['last_receipt'] is None for x in other['merchants'].values())


def test_final_independent_sources_agree_after_transfer_and_pickup(system):
    st,pg=system;s=ordered(st);oid=s['order']['id']
    s=command(st,s,'transfer',quote_id=plan(s,'oat')['quote_id'])
    s=ready(st,s);s=command(st,s,'claim',pickup_code=s['order']['pickup_code'])
    out=check(st,s)
    assert out['status']=='MATCH' and out['terminal']
    assert out['order_id']==oid and set(out['checks'])=={'order','merchant','payment','benefits'}
    assert all(c['status']=='MATCH' for c in out['checks'].values())
    assert out['payment']['captured_krw']==3200 and out['payment']['held_krw']==0
    assert out['merchants']['oat']['reservation']['phase']=='CLAIMED'
    assert out['merchants']['wave']['reservation']['phase']=='RELEASED'
    assert out['observation']['stable'] and out['observation']['coordinator_version']==s['version']
    assert 'secret' not in json.dumps(out) and 'demo-approved' not in json.dumps(out)


def test_payment_unavailable_is_not_zero_or_a_green_result(system):
    st,pg=system;s=ordered(st)
    class Unavailable:
        def snapshot(self,*args):raise OSError('private internal URL and token must not leak')
    st.payment_gateway=Unavailable();out=check(st,s)
    assert out['status']=='UNAVAILABLE' and out['payment'] is None
    assert out['checks']['payment']['status']=='UNAVAILABLE'
    assert 'private internal' not in json.dumps(out)


def test_financial_corruption_is_detected_even_if_order_receipt_still_says_success(system):
    st,pg=system;s=ready(st,ordered(st));s=command(st,s,'claim',pickup_code=s['order']['pickup_code'])
    with st.connection() as db:
        raw=st._load(db,s['id']);raw['order']['price']+=100;st._save(db,raw)
    out=check(st,s)
    assert out['status']=='MISMATCH' and out['checks']['payment']['status']=='MISMATCH'
    assert out['payment']['captured_krw']==2800


def test_reading_evidence_does_not_recover_or_consume_notifications(system):
    st,pg=system;s=ordered(st,payment_fault='authorize_reply_lost')
    assert s['handoff_pending'] and s['order'] is None
    before=st.get(s['id']);out=check(st,s);after=st.get(s['id'])
    assert before==after
    assert out['status']=='PENDING' and out['order_id']==s['pending_order_id']
    assert out['payment']['held_krw']==2800 and not out['terminal']


def test_changed_source_revision_is_pending_not_inconsistent_success(system):
    st,pg=system;s=ordered(st);gateway=st.payment_gateway
    class Moving:
        def __init__(self):self.calls=0
        def snapshot(self,*args):
            self.calls+=1;out=gateway.snapshot(*args);out['revision']+=self.calls
            return out
    st.payment_gateway=Moving();out=check(st,s)
    assert out['status']=='PENDING' and not out['observation']['stable']


def test_cancelled_order_has_no_charge_hold_or_consumed_benefits(system):
    st,pg=system;s=ordered(st);s=command(st,s,'cancel');out=check(st,s)
    assert out['status']=='MATCH' and out['terminal']
    assert out['payment']['held_krw']==out['payment']['captured_krw']==0
    assert out['checks']['benefits']['status']=='MATCH'


def test_legacy_mode_cannot_claim_external_payment_proof(system):
    st,pg=system;st.payment_enabled=False;s=ordered(st);out=check(st,s)
    assert out['status']=='UNAVAILABLE' and out['payment'] is None
    assert out['scope']=='legacy_local_simulation'
