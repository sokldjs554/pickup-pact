from copy import deepcopy
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from demo.main import app
from demo.route import api as route_api
from demo.route.store import JourneyStore

@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(route_api,'store',JourneyStore(tmp_path/'comparison.sqlite'))

C=TestClient(app)

def request(action,s,**kw):
    r=C.post(f"/api/route/journeys/{s['id']}/commands",json=dict(action=action,expected_version=s['version'],request_id=uuid4().hex,**kw))
    assert r.status_code==200,r.text
    return r.json()

def scenario():
    s=C.post('/api/route/journeys',json={}).json()
    s=request('reserve',s,quote_id=s['recommendations'][0]['quote_id'])
    return request('disrupt',s,store_id=s['order']['store_id'],minutes=12)

def test_current_comparison_uses_same_stored_order_and_does_not_modify_it():
    s=scenario();url=f"/api/route/journeys/{s['id']}"
    r=C.get(url+'/comparison');assert r.status_code==200,r.text
    out=r.json()
    assert out['basis']=='synthetic_snapshot'
    assert out['version']==s['version']
    assert out['stay']['arrival_at']==20
    assert out['alternatives'][0]['arrival_at']==9
    assert out['alternatives'][0]['minutes_saved']==11
    assert out['alternatives'][0]['cash_delta']==400
    assert C.get(url).json()==s


def test_experiment_endpoint_reproduces_raw_paired_cases_and_all_denominators():
    response=C.post('/api/route/experiments',json={'seed':7,'cases':60})
    assert response.status_code==200,response.text
    a=response.json();b=C.post('/api/route/experiments',json={'seed':7,'cases':60}).json()
    assert a==b
    assert len(a['cases'])==a['summary']['total']==60
    assert a['summary']['wins']+a['summary']['ties']+a['summary']['losses']==60
    assert set(a['summary']['by_scenario'])=={'normal','source_busy','all_busy','after_start','coupon_budget','uncertain_delay'}
    for case in a['cases']:
        assert case['stay']['environment_hash']==case['transfer']['environment_hash']
        assert case['stay']['initial_store']==case['transfer']['initial_store']
        assert case['stay']['initially_serviceable']==case['transfer']['initially_serviceable']
        if case['transferred']:
            assert case['state_at_decision']=='RESERVED'
            assert case['transfer']['cash_due']<=case['intent']['budget']
        if case['scenario']=='after_start':
            assert not case['transferred']
    assert sum(x['stay']['on_time'] for x in a['cases'])==a['summary']['stay_on_time']
    assert sum(x['transfer']['on_time'] for x in a['cases'])==a['summary']['transfer_on_time']

@pytest.mark.parametrize('body',[{'cases':0},{'cases':201},{'cases':True},{'seed':-1},{'seed':'7'}])
def test_trial_input_is_bounded(body):
    assert C.post('/api/route/experiments',json=body).status_code==422


def test_policy_cannot_see_hidden_realization_and_bad_cases_are_not_filtered():
    # Imported inside test so the initial RED is an ordinary missing-feature assertion.
    import importlib.util
    assert importlib.util.find_spec('demo.route.outcomes') is not None
    from demo.route.outcomes import choose_transfer, evaluate_case, make_case
    case=make_case(19,5)
    changed=deepcopy(case)
    changed['future_delays']={key:30 for key in case['future_delays']}
    assert choose_transfer(case['observed'])==choose_transfer(changed['observed'])
    assert evaluate_case(case)['transfer']['arrival_at']!=evaluate_case(changed)['transfer']['arrival_at']
    blocked=make_case(7,2)
    assert evaluate_case(blocked)['scenario']=='all_busy'
    assert not evaluate_case(blocked)['transferred']


def test_selected_policy_quotes_are_accepted_by_real_journey_transaction(tmp_path):
    from demo.route.outcomes import make_case, choose_transfer, evaluate_case
    store=JourneyStore(tmp_path/'policy.sqlite');exercised=0
    for i in range(60):
        case=make_case(7,i);s=case['observed'];target,reason=choose_transfer(s)
        if target is None: continue
        with store.connection() as db: store._save(db,s)
        view=store.get(s['id']);q=next(p for p in view['all_plans'] if p['store_id']==target)
        result=store.command(s['id'],dict(action='transfer',quote_id=q['quote_id'],expected_version=s['version'],request_id=f'transfer-{i}'))
        predicted=evaluate_case(case)
        assert result['order']['price']==predicted['transfer']['cash_due']
        assert result['current_plan']['arrival_at']==predicted['transfer']['estimated_arrival']
        assert result['wallet']['balance']-result['wallet']['held_points']==result['wallet']['available_points']
        assert result['order']['id']==s['order']['id'];exercised+=1
    assert exercised>0


def test_reproducible_uncertainty_suite_exposes_a_regression_not_only_wins():
    from demo.route.outcomes import run_experiment
    report=run_experiment(7,120)
    assert report['summary']['losses']>0
    assert report['summary']['no_initial_route']>0
    for row in report['cases']:
        if row['decision_reason'] in {'ALREADY_PREPARING','NO_ELIGIBLE_ALTERNATIVE'}:
            assert row['stay']==row['transfer']
