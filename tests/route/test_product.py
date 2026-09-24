from fastapi.testclient import TestClient
from demo.main import app

def test_time_first_order_is_a_real_server_endpoint():
    response = TestClient(app).post('/api/route/journeys', json={
        'destination': 'office', 'deadline_minutes': 16, 'drink': 'latte',
        'milk': 'regular', 'decaf': False, 'budget': 5500, 'max_detour': 5,
        'priority': 'arrival',
    })
    assert response.status_code == 201, response.text
    body = response.json()
    assert body['id']
    assert len(body['recommendations']) >= 2
    assert all(p['feasible'] for p in body['recommendations'])

def test_new_product_is_home_and_legacy_experience_is_preserved():
    c=TestClient(app)
    assert '커피는 챙기고' in c.get('/').text
    assert '오늘 뭐 드실래요?' in c.get('/classic').text
    assert c.get('/repair-lab').status_code==200
    for asset in ['product.css','product.js']:
        assert c.get('/route-assets/'+asset).status_code==200
    assert c.get('/route-assets/not-allowed').status_code==404
    assert '/api/route/journeys' in c.get('/openapi.json').json()['paths']
