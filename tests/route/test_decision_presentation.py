from fastapi.testclient import TestClient
from demo.main import app


def test_home_puts_order_protection_before_generic_coffee_message():
    text=TestClient(app).get('/').text
    assert '매장 변경이 막혀도' in text and '내 주문은' in text
    assert '360mL' in text and '맛은 매장마다' in text
    assert 'agreement-ui.js' in text and 'selection-state.js' in text


def test_comparison_shows_stronger_baseline_and_explicit_ties():
    text=TestClient(app).get('/route-assets/handoff-ui.js').text
    assert '새 자리 확보 후 재주문' in text
    assert 'reserve_first_reorder' in text
    assert '같은 안전 절차' in text


def test_terms_and_settlement_display_have_real_server_sources():
    response=TestClient(app).get('/route-assets/agreement-ui.js')
    assert response.status_code==200
    text=response.text
    assert 'transfer_terms' in text and 'receipt.settlement' in text
    assert 'merchant_receivable' in text and 'platform_coupon' in text
