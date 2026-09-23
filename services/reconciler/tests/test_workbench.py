from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime
from fastapi.testclient import TestClient
import pytest
from app.workbench import create_app

@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(tmp_path/'review.sqlite'))

def create(client, case='delayed_cancel'):
    r=client.post('/api/repair-lab/sessions',json={'case':case})
    assert r.status_code==201,r.text
    return r.json()

def preview(client, session):
    r=client.post(f"/api/repair-lab/sessions/{session['id']}/plans",json={'version':session['version']})
    assert r.status_code==201,r.text
    return r.json()

def approve(client, session, plan, actions=None):
    return client.post(f"/api/repair-lab/sessions/{session['id']}/plans/{plan['id']}/approve",
        json={'version':plan['version'],'evidence_hash':plan['evidence_hash'],
              'actions':plan['actions'] if actions is None else actions})

def test_landing_is_actual_review_interface_not_static_report(client):
    r=client.get('/repair-lab')
    assert r.status_code==200
    assert '실제 결제·환불을 실행하지 않습니다' in r.text
    assert '승인 후 모의 기록' in r.text

def test_real_engine_result_is_shown_with_evidence(client):
    s=create(client)
    assert s['evaluation']['decision']=='AUTO'
    assert s['evaluation']['canonical_state']['status']=='CANCELLED'
    assert len(s['events'])==6
    assert s['effects']==[]

def test_explicit_plan_binds_event_amount_unit_and_exact_evidence(client):
    s=create(client); p=preview(client,s)
    assert sorted((a['target_event_id'],a['amount'],a['unit']) for a in p['actions'])==[
        ('reward',90,'PTS'),('settle',9000,'KRW')]
    r=approve(client,s,p)
    assert r.status_code==200,r.text
    assert len(r.json()['session']['effects'])==2
    assert r.json()['session']['evaluation']['decision']=='AUTO'
    assert not {'REVERSE_SETTLEMENT','REVERSE_REWARD'} & set(r.json()['session']['evaluation']['repairs'])

@pytest.mark.parametrize('case',['conflicting_identity','terminal_conflict'])
def test_unsafe_cases_have_no_executable_plan(client,case):
    s=create(client,case)
    assert s['evaluation']['decision']=='MANUAL_REVIEW'
    r=client.post(f"/api/repair-lab/sessions/{s['id']}/plans",json={'version':s['version']})
    assert r.status_code==409
    assert client.get(f"/api/repair-lab/sessions/{s['id']}").json()['effects']==[]

def test_partial_cancellation_plan_uses_only_explicit_allocation(client):
    s=create(client,'partial_cancel')
    assert s['evaluation']['decision']=='AUTO'
    assert s['evaluation']['canonical_state']['status']=='CONFIRMED'
    p=preview(client,s)
    assert [(a['target_event_id'],a['amount'],a['unit']) for a in p['actions']] == [
        ('settle',3000,'KRW')
    ]
    result=approve(client,s,p)
    assert result.status_code==200,result.text
    effects=result.json()['session']['effects']
    assert [(e['target_event_id'],e['amount'],e['unit']) for e in effects] == [
        ('settle',3000,'KRW')
    ]


def test_partial_cancellation_is_not_planned_twice_after_approval(client):
    s=create(client,'partial_cancel')
    p=preview(client,s)
    result=approve(client,s,p)
    assert result.status_code==200,result.text
    state=result.json()['session']
    assert state['evaluation']['financial_actions']==[]
    assert not {'REVERSE_SETTLEMENT','REVERSE_REWARD'} & set(state['evaluation']['repairs'])
    retry_plan=client.post(
        f"/api/repair-lab/sessions/{s['id']}/plans",
        json={'version':state['version']},
    )
    assert retry_plan.status_code==409


def test_full_cancellation_multiple_postings_records_each_open_balance_once(client):
    s=create(client,'multiple_postings')
    assert s['evaluation']['decision']=='AUTO'
    p=preview(client,s)
    assert sorted((a['target_event_id'],a['amount'],a['unit']) for a in p['actions']) == [
        ('settle',9000,'KRW'),
        ('settle2',2000,'KRW'),
    ]
    result=approve(client,s,p)
    assert result.status_code==200,result.text
    assert len(result.json()['session']['effects'])==2


def test_missing_parent_stays_waiting_until_evidence_arrives(client):
    s=create(client,'missing_parent')
    assert s['evaluation']['decision']=='WAIT_FOR_EVIDENCE'
    assert s['evaluation']['missing_event_ids']==['late']
    r=client.post(f"/api/repair-lab/sessions/{s['id']}/simulate",json={'action':'deliver_missing'})
    assert r.status_code==200,r.text
    s=r.json(); assert s['evaluation']['decision']=='AUTO'
    assert len(preview(client,s)['actions'])==1

def test_stale_plan_is_rejected_after_conflict_arrives(client):
    s=create(client); p=preview(client,s)
    r=client.post(f"/api/repair-lab/sessions/{s['id']}/simulate",json={'action':'conflict'})
    assert r.status_code==200
    assert r.json()['version']>p['version']
    assert approve(client,s,p).status_code==409
    assert client.get(f"/api/repair-lab/sessions/{s['id']}").json()['effects']==[]

@pytest.mark.parametrize('field,value',[('amount',1),('unit','PTS'),('target_event_id','different')])
def test_changed_approval_payload_is_rejected(client,field,value):
    s=create(client); p=preview(client,s); altered=deepcopy(p['actions'])
    a=next(a for a in altered if a['target_event_id']=='settle'); a[field]=value
    assert approve(client,s,p,altered).status_code==409
    assert client.get(f"/api/repair-lab/sessions/{s['id']}").json()['effects']==[]

def test_same_evidence_redelivery_does_not_change_plan_version(client):
    s=create(client); p=preview(client,s)
    r=client.post(f"/api/repair-lab/sessions/{s['id']}/simulate",json={'action':'redeliver'})
    assert r.status_code==200
    assert r.json()['version']==s['version']
    assert approve(client,s,p).status_code==200

def test_duplicate_approvals_converge_to_single_effects(client):
    s=create(client); p=preview(client,s)
    with ThreadPoolExecutor(max_workers=8) as pool:
        rs=list(pool.map(lambda _:approve(client,s,p),range(24)))
    assert {r.status_code for r in rs}=={200}
    state=client.get(f"/api/repair-lab/sessions/{s['id']}").json()
    assert len(state['effects'])==2
    assert sum(1 for r in rs if not r.json()['duplicate'])==1

def test_plan_cannot_be_used_in_another_session(client):
    a=create(client); p=preview(client,a); b=create(client)
    assert approve(client,b,p).status_code==404

def test_restart_preserves_evidence_plans_and_deduplication(tmp_path):
    path=tmp_path/'review.sqlite'; first=TestClient(create_app(path))
    s=create(first); p=preview(first,s); assert approve(first,s,p).status_code==200
    second=TestClient(create_app(path)); retry=approve(second,s,p)
    assert retry.status_code==200
    assert retry.json()['duplicate'] is True
    assert len(retry.json()['session']['effects'])==2

def test_unknown_scenario_does_not_silently_load_a_safe_default(client):
    assert client.post('/api/repair-lab/sessions',json={'case':'typo'}).status_code==422

def test_schema_lists_approval_contract(client):
    schema=client.get('/openapi.json').json()
    assert '/api/repair-lab/sessions/{session_id}/plans/{plan_id}/approve' in schema['paths']

@pytest.mark.parametrize('crash',['after_events','before_commit','after_commit'])
def test_process_termination_preserves_atomic_plan_and_effects(tmp_path,crash):
    import json, os, subprocess, sys
    from app.repair_review_store import ReviewStore
    path=tmp_path/'crash.sqlite'; c=TestClient(create_app(path))
    s=create(c); p=preview(c,s)
    script=r'''
import json,os,sqlite3,sys
from app.repair_review_store import ReviewStore
path,sid,raw,mode=sys.argv[1:]
p=json.loads(raw)
store=ReviewStore(path)
if mode=='after_events':
    original=store._add_events
    def add(*args):
        original(*args)
        os._exit(77)
    store._add_events=add
else:
    class Connection(sqlite3.Connection):
        def commit(self):
            if mode=='before_commit': os._exit(77)
            super().commit()
            os._exit(77)
    def connect():
        db=sqlite3.connect(path,timeout=10,factory=Connection)
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA synchronous=FULL')
        return db
    store._connect=connect
store.approve(sid,p['id'],p['version'],p['evidence_hash'],p['actions'])
'''
    proc=subprocess.run([sys.executable,'-c',script,str(path),s['id'],json.dumps(p),crash],
        capture_output=True,text=True,timeout=15)
    assert proc.returncode==77,proc.stderr
    restored=TestClient(create_app(path))
    state=restored.get(f"/api/repair-lab/sessions/{s['id']}").json()
    assert len(state['effects'])==(2 if crash=='after_commit' else 0)
    assert len(state['events'])==(8 if crash=='after_commit' else 6)
    retry=approve(restored,s,p)
    assert retry.status_code==200,retry.text
    assert len(retry.json()['session']['effects'])==2
    assert retry.json()['duplicate'] is (crash=='after_commit')


def test_cross_origin_mutation_is_rejected(client):
    r=client.post('/api/repair-lab/sessions',json={'case':'delayed_cancel'},headers={'Origin':'https://other.example'})
    assert r.status_code==403


def test_plan_becomes_stale_when_another_plan_commits(client):
    s=create(client); first=preview(client,s); second=preview(client,s)
    assert approve(client,s,first).status_code==200
    assert approve(client,s,second).status_code==409
    assert len(client.get(f"/api/repair-lab/sessions/{s['id']}").json()['effects'])==2


def test_applied_plan_does_not_accept_tampering_on_retry(client):
    s=create(client); p=preview(client,s); assert approve(client,s,p).status_code==200
    actions=deepcopy(p['actions']); actions[0]['amount']=123
    assert approve(client,s,p,actions).status_code==409
