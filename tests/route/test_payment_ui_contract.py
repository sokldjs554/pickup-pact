from pathlib import Path
from fastapi.testclient import TestClient
from demo.main import app
from demo.route import api
from demo.route.store import JourneyStore


def test_payment_proof_and_two_terminals_are_available_without_real_card_input(monkeypatch,tmp_path):
    monkeypatch.setattr(api,'store',JourneyStore(tmp_path/'orders.sqlite'))
    with TestClient(app) as client:
        html=client.get('/').text
        for target in ['id="paymentCard"','id="paymentScenario"','id="paymentEvidence"','id="merchantTerminals"','id="finalProof"','payment-ui.js','payment.css']:
            assert target in html,target
        assert 'name="card_number"' not in html and 'name="cvc"' not in html
        for name in ['payment-ui.js','payment.css']:
            r=client.get('/route-assets/'+name);assert r.status_code==200
            assert r.headers['x-content-type-options']=='nosniff'
        s=client.post('/api/route/journeys',json={}).json()
        r=client.get('/api/route/journeys/'+s['id']+'/reconciliation')
        assert r.status_code==200 and r.json()['status']=='PENDING'
        assert r.headers['cache-control']=='no-store'
        assert client.get('/api/route/journeys/00000000-0000-0000-0000-000000000000/reconciliation').status_code==404


def test_reconciliation_api_is_explicitly_typed():
    schema=app.openapi()
    path='/api/route/journeys/{journey_id}/reconciliation'
    assert path in schema['paths']
    response=schema['paths'][path]['get']['responses']['200']['content']['application/json']['schema']
    assert response['$ref'].endswith('/ReconciliationView')
    assert set(schema['components']['schemas']['EvidenceCheck']['properties']['status']['enum'])=={'MATCH','PENDING','MISMATCH','UNAVAILABLE'}
