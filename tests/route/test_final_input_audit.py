"""Reject malformed Unicode before command hashing or validation error rendering."""
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from demo.main import app
from demo.route import api as route_api
from demo.route.store import JourneyStore


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(route_api, 'store', JourneyStore(tmp_path / 'input-audit.sqlite'))


@pytest.mark.parametrize('bad', ['\ud800', '\udfff', '12\ud80034'])
@pytest.mark.parametrize('target', ['pickup_code', 'quote_id', 'unknown_key', 'intent', 'experiment'])
def test_lone_surrogate_is_a_clean_bad_request_and_does_not_mutate(bad, target):
    client = TestClient(app, raise_server_exceptions=False)
    s = client.post('/api/route/journeys', json={}).json()
    url = f"/api/route/journeys/{s['id']}/commands"
    payload = dict(action='claim', expected_version=s['version'], request_id=uuid4().hex, pickup_code='123456')
    if target == 'intent':
        url = '/api/route/journeys'; payload = {'drink': bad}
    elif target == 'experiment':
        url = '/api/route/experiments'; payload = {'seed': bad}
    elif target == 'unknown_key':
        payload[bad] = ['nested', {'value': bad}]
    elif target == 'quote_id':
        payload = dict(action='reserve', expected_version=s['version'], request_id=uuid4().hex, quote_id=bad)
    else:
        payload['pickup_code'] = bad
    result = client.post(url, content=json.dumps(payload, ensure_ascii=True), headers={'Content-Type':'application/json'})
    assert result.status_code == 400, (target, repr(bad), result.status_code, result.text)
    assert result.json()['detail'] == 'invalid Unicode in JSON'
    assert client.get(f"/api/route/journeys/{s['id']}").json() == s


def test_well_formed_unicode_keeps_existing_validation_contract():
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post('/api/route/journeys', json={'drink':'커피☕'})
    assert response.status_code == 422
    assert client.post('/api/route/journeys', json={}).status_code == 201


@pytest.mark.parametrize('raw,expected', [('',422), ('null',422), ('[]',422), ('{',422), ('"valid text"',422), ('{}',201)])
def test_json_shape_validation_keeps_existing_contract(raw, expected):
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post('/api/route/journeys', content=raw, headers={'Content-Type':'application/json'})
    assert response.status_code == expected, (raw, response.status_code, response.text)
