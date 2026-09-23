"""Actual ledger HTTP+PostgreSQL behavior, no mocked repository or HTTP transport."""
from concurrent.futures import ThreadPoolExecutor
import os
from uuid import uuid4
import httpx
import pytest

BASE=os.getenv('LEDGER_TEST_URL')
pytestmark=pytest.mark.skipif(not BASE,reason='requires running ledger service and dedicated PostgreSQL')


def req(method,path,body=None,expected=200):
    r=httpx.request(method,BASE+path,json=body,timeout=12)
    assert r.status_code==expected,(r.status_code,r.text)
    return r.json()


def source(aggregate,amount,kind='SETTLEMENT',event_id=None):
    eid=event_id or str(uuid4())
    req('POST','/api/v1/ledger/postings',dict(eventId=eid,aggregateId=aggregate,type=kind,amount=amount),202)
    return eid


def plan(aggregate,amount,kind='SETTLEMENT'):
    return req('POST',f'/api/v1/ledger/orders/{aggregate}/refund-plans',
               dict(requestId=str(uuid4()),type=kind,amount=amount),201)


def approve(aggregate,p,expected=200,**changes):
    body={k:p[k] for k in ('sourceRevision','evidenceHash','allocations')}
    body.update(changes)
    return req('POST',f"/api/v1/ledger/orders/{aggregate}/refund-plans/{p['id']}/approve",body,expected)


def test_partial_amount_allocates_source_lines_and_leaves_exact_remaining_balance():
    agg=str(uuid4()); a=source(agg,6000); b=source(agg,5000)
    p=plan(agg,10000)
    assert [(x['sourceEventId'],x['amount'],x['unit']) for x in p['allocations']]==[(a,6000,'KRW'),(b,4000,'KRW')]
    result=approve(agg,p)
    assert result['duplicate'] is False
    assert len(result['reversalEventIds'])==2
    assert approve(agg,p)['duplicate'] is True
    rest=plan(agg,1000)
    assert [(x['sourceEventId'],x['amount']) for x in rest['allocations']]==[(b,1000)]
    approve(agg,rest)
    req('POST',f'/api/v1/ledger/orders/{agg}/refund-plans',
        dict(requestId=str(uuid4()),type='SETTLEMENT',amount=1),409)
    rows=req('GET',f'/api/v1/ledger/orders/{agg}?limit=100')
    assert sum(x['amount'] for x in rows if x['reason']=='REVERSE_SETTLEMENT')==11000
    assert len(rows)==5


def test_duplicate_approval_24_requests_has_single_committed_receipt():
    agg=str(uuid4()); source(agg,9000); p=plan(agg,3000)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(lambda _:approve(agg,p),range(24)))
    assert sum(not r['duplicate'] for r in results)==1
    assert len({tuple(r['reversalEventIds']) for r in results})==1
    rows=req('GET',f'/api/v1/ledger/orders/{agg}?limit=100')
    assert len(rows)==2


def test_two_plans_for_same_balance_cannot_both_commit():
    agg=str(uuid4()); source(agg,9000); first=plan(agg,6000); second=plan(agg,6000)
    def apply(p):
        return httpx.post(BASE+f"/api/v1/ledger/orders/{agg}/refund-plans/{p['id']}/approve",
            json={k:p[k] for k in ('sourceRevision','evidenceHash','allocations')},timeout=12)
    with ThreadPoolExecutor(max_workers=2) as pool:
        rs=list(pool.map(apply,[first,second]))
    assert sorted(r.status_code for r in rs)==[200,409]
    assert sum(x['amount'] for x in req('GET',f'/api/v1/ledger/orders/{agg}') if x['reason']=='REVERSE_SETTLEMENT')==6000


def test_new_posting_makes_saved_plan_stale_even_if_it_increases_balance():
    agg=str(uuid4()); source(agg,9000); p=plan(agg,3000); source(agg,1000)
    approve(agg,p,expected=409)
    assert len(req('GET',f'/api/v1/ledger/orders/{agg}'))==2


def test_approval_cannot_change_target_amount_or_unit():
    agg=str(uuid4()); source(agg,9000); p=plan(agg,3000)
    for key,value in [('amount',3001),('unit','PTS'),('sourceEventId','wrong')]:
        modified=[{**p['allocations'][0],key:value}]
        approve(agg,p,expected=409,allocations=modified)
    assert len(req('GET',f'/api/v1/ledger/orders/{agg}'))==1


def test_unattributed_legacy_reversal_blocks_new_allocation():
    agg=str(uuid4()); source(agg,9000); source(agg,2000,kind='REVERSE_SETTLEMENT')
    req('POST',f'/api/v1/ledger/orders/{agg}/refund-plans',
        dict(requestId=str(uuid4()),type='SETTLEMENT',amount=1),409)


def test_reward_and_settlement_are_separate_approved_unit_budgets():
    agg=str(uuid4()); source(agg,9000); point=source(agg,90,kind='REWARD')
    p=plan(agg,30,kind='REWARD')
    assert p['allocations']==[{'sourceEventId':point,'amount':30,'unit':'PTS'}]
    approve(agg,p)
    rows=req('GET',f'/api/v1/ledger/orders/{agg}')
    assert sum(x['amount'] for x in rows if x['reason']=='REVERSE_REWARD')==30
    assert not any(x['reason']=='REVERSE_SETTLEMENT' for x in rows)


def test_request_id_is_idempotent_and_payload_change_conflicts():
    agg=str(uuid4()); source(agg,9000)
    body=dict(requestId=str(uuid4()),type='SETTLEMENT',amount=1000)
    path=f'/api/v1/ledger/orders/{agg}/refund-plans'
    a=req('POST',path,body,201);b=req('POST',path,body,201)
    assert a['id']==b['id']
    req('POST',path,{**body,'amount':2000},409)


def test_fractional_and_boolean_amount_are_not_accepted():
    agg=str(uuid4()); source(agg,9000)
    for amount in [1.25,True,0,-1,1000000000001]:
        r=httpx.post(BASE+f'/api/v1/ledger/orders/{agg}/refund-plans',
            json=dict(requestId=str(uuid4()),type='SETTLEMENT',amount=amount),timeout=12)
        assert r.status_code==400,(amount,r.text)
