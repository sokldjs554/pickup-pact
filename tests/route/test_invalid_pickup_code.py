"""User-entered pickup text must never produce a server error or capture."""
import pytest
from fastapi.testclient import TestClient
from demo.main import app
from test_journey import ordered, accepted

@pytest.mark.parametrize('code', ['가나다', '☕', '１２３４５６'])
def test_non_ascii_pickup_code_is_rejected_without_capturing(code):
    s = ordered()
    s = accepted(s, 'advance', minutes=max(0, s['order']['plan']['start_at'] - s['clock']))
    s = accepted(s, 'start')
    s = accepted(s, 'advance', minutes=s['order']['ready_at'] - s['clock'])
    s = accepted(s, 'ready')
    client = TestClient(app, raise_server_exceptions=False)
    url = f"/api/route/journeys/{s['id']}"
    response = client.post(url + '/commands', json={
        'action': 'claim', 'expected_version': s['version'],
        'request_id': 'non-ascii', 'pickup_code': code,
    })
    assert response.status_code == 409, response.text
    after = client.get(url).json()
    assert after['version'] == s['version']
    assert after['order']['state'] == 'READY'
    assert after['receipt']['capture_count'] == 0
    assert after['events'] == s['events']
