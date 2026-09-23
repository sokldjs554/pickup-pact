"""Exercise the deployed entrypoint, not a mock router or static screenshot."""
import importlib
import os
from pathlib import Path

from fastapi.testclient import TestClient
import pytest


@pytest.fixture(scope='module')
def client(tmp_path_factory):
    db = tmp_path_factory.mktemp('integrated-review')/'review.sqlite'
    previous = os.environ.get('REPAIR_REVIEW_DB')
    os.environ['REPAIR_REVIEW_DB'] = str(db)
    try:
        module = importlib.import_module('demo.review_app')
        with TestClient(module.app) as client:
            yield client
    finally:
        if previous is None:
            os.environ.pop('REPAIR_REVIEW_DB', None)
        else:
            os.environ['REPAIR_REVIEW_DB'] = previous


def test_customer_home_and_repair_page_both_exist(client):
    assert client.get('/').status_code == 200
    assert '매장 운영' in client.get('/').text
    review = client.get('/repair-lab')
    assert review.status_code == 200
    assert '실제 결제·환불을 실행하지 않습니다' in review.text
    assert client.get('/health').json()['service'] == 'pickup-pact-demo'


def test_deployed_entrypoint_runs_real_plan_approval_and_retry(client):
    created = client.post('/api/repair-lab/sessions', json={'case':'delayed_cancel'})
    assert created.status_code == 201, created.text
    sample = created.json()
    endpoint = f"/api/repair-lab/sessions/{sample['id']}"
    plan = client.post(endpoint+'/plans',json={'version':sample['version']})
    assert plan.status_code == 201, plan.text
    plan = plan.json()
    body = {key:plan[key] for key in ['version','evidence_hash','actions']}
    uri = endpoint+f"/plans/{plan['id']}/approve"
    first = client.post(uri,json=body)
    assert first.status_code == 200, first.text
    assert len(first.json()['session']['effects']) == 2
    assert client.post(uri,json=body).json()['duplicate'] is True


def test_original_customer_sessions_remain_usable(client):
    session = client.post('/api/demo/sessions')
    assert session.status_code == 200
    assert session.json()['session_id']


def test_integrated_router_retains_origin_and_json_guard(client):
    denied = client.post('/api/repair-lab/sessions',json={'case':'normal'},
        headers={'Origin':'https://other.example'})
    assert denied.status_code == 403
    denied = client.post('/api/repair-lab/sessions',content='{"case":"normal"}',
        headers={'Content-Type':'text/plain'})
    assert denied.status_code == 415


def test_deployed_schema_exposes_approval_contract(client):
    schema = client.get('/openapi.json').json()
    assert '/api/demo/sessions' in schema['paths']
    assert '/api/repair-lab/sessions/{session_id}/plans/{plan_id}/approve' in schema['paths']
    assert set(schema['components']['schemas']['ApproveRequest']['required']) == {
        'version','evidence_hash','actions'}
    assert schema['components']['schemas']['ApprovedAction']['additionalProperties'] is False


def test_render_and_docker_run_integrated_entrypoint():
    root = Path(__file__).resolve().parents[1]
    assert 'demo.review_app:app' in (root/'render.yaml').read_text()
    assert 'demo.review_app:app' in (root/'Dockerfile.demo').read_text()


def test_legacy_start_command_also_exposes_repair_routes(client):
    from demo.main import app as original_app
    legacy = TestClient(original_app)
    assert legacy.get('/repair-lab').status_code == 200
    assert legacy.post('/api/repair-lab/sessions',json={'case':'normal'}).status_code == 201
