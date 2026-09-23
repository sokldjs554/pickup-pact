from fastapi.testclient import TestClient
import pytest
from app.workbench import create_app

@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(tmp_path/'review.sqlite'))


def test_router_keeps_mutation_guard_when_mounted_in_existing_app(tmp_path):
    from fastapi import FastAPI
    from app.workbench import create_router
    parent = FastAPI()
    parent.include_router(create_router(tmp_path/'mounted.sqlite'))
    client = TestClient(parent)
    blocked = client.post('/api/repair-lab/sessions', json={'case':'normal'},
        headers={'Origin':'https://untrusted.example'})
    assert blocked.status_code == 403
    valid = client.post('/api/repair-lab/sessions',json={'case':'normal'})
    assert valid.status_code == 201
    assert valid.headers['cache-control'] == 'no-store'


def test_router_rejects_non_json_posts_when_mounted(tmp_path):
    from fastapi import FastAPI
    from app.workbench import create_router
    parent = FastAPI(); parent.include_router(create_router(tmp_path/'mounted.sqlite'))
    client = TestClient(parent)
    r=client.post('/api/repair-lab/sessions',content='{"case":"normal"}',
        headers={'Content-Type':'text/plain'})
    assert r.status_code == 415


def test_openapi_documents_concrete_session_plan_and_approval_responses(client):
    schema=client.get('/openapi.json').json()
    paths=schema['paths']
    session_response=paths['/api/repair-lab/sessions']['post']['responses']['201']
    assert session_response['content']['application/json']['schema'].get('$ref') == '#/components/schemas/SessionView'
    approval=paths['/api/repair-lab/sessions/{session_id}/plans/{plan_id}/approve']['post']
    assert approval['responses']['200']['content']['application/json']['schema'].get('$ref') == '#/components/schemas/ApprovalView'
    assert {'403','404','409','415','422'} <= set(approval['responses'])
