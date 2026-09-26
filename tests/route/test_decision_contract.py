from copy import deepcopy
from time import perf_counter
from fastapi.testclient import TestClient
import pytest
from demo.main import app
from demo.route import api
from demo.route.api import Intent
from demo.route.store import JourneyStore


def test_core_response_schemas_are_named_and_nested():
    doc=app.openapi()
    for path,method in [('/api/route/journeys','post'),('/api/route/journeys/{journey_id}','get'),
                        ('/api/route/transfer-comparison','post'),('/api/route/runtime','get'),
                        ('/api/route/journeys/{journey_id}/receipt','get')]:
        responses=doc['paths'][path][method]['responses']
        schema=responses['201' if path=='/api/route/journeys' else '200']['content']['application/json']['schema']
        assert '$ref' in schema,(path,schema)
    assert 'TransferTerms' in doc['components']['schemas']
    assert 'FundingBreakdown' in doc['components']['schemas']


def test_valid_responses_preserve_shape_and_reject_invalid_money(tmp_path,monkeypatch):
    from demo.route.response_models import JourneyView
    from pydantic import ValidationError
    st=JourneyStore(tmp_path/'orders.sqlite');monkeypatch.setattr(api,'store',st)
    s=st.create(Intent().model_dump())
    assert JourneyView.model_validate(s).model_dump(exclude_unset=True)==s
    bad=deepcopy(s);bad['all_plans'][0]['transfer_terms']['funding']['customer_cash']+=1
    with pytest.raises(ValidationError):JourneyView.model_validate(bad)
    c=TestClient(app)
    assert c.get('/api/route/journeys/'+s['id']).json()==s


def test_comparison_reuses_setup_without_sharing_scenario_capacity(monkeypatch,tmp_path):
    import demo.route.transfer_comparison as mod
    from demo.route.store import JourneyStore
    count=0
    init=JourneyStore.__init__
    def observed(self,*args,**kwargs):
        nonlocal count
        count+=1
        return init(self,*args,**kwargs)
    monkeypatch.setattr(JourneyStore,'__init__',observed)
    intent=Intent(coupon_id='welcome500',points=1000).model_dump()
    report=mod.run_transfer_comparison(intent)
    assert count==1
    assert len(report['executions'])==24
    assert report['timing']['isolated_worlds']==24
    normal=next(r for r in report['cases'] if r['scenario']=='normal')
    assert all(normal[p]['has_order'] for p in report['policies'])
    assert report['timing']['storage_durability']=='SQLite WAL, synchronous FULL (merchant)'


@pytest.mark.parametrize('seed,expected',[(7, 'f2dcf99d54ca8808b1228c24255c9561a937b2d29876ab4e59f0ae9dc931d2e4'), (19, '1288ce15e815f5d78d8669597b2a1d9cd6692e76267c09159db496f0226ad952'), (31, '7a4fd0a294bc4bb4466a6a238a2c1deee268e6e035a78f4c2eed2d304c89a95e')])
def test_legacy_arrival_experiment_keeps_its_original_population(seed,expected):
    from demo.route.outcomes import run_experiment
    assert run_experiment(seed,120)['semantic_sha256']==expected
