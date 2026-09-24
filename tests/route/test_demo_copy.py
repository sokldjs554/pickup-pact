"""Copy contracts: readable Korean, with the simulation boundary kept visible."""
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from demo.main import app

ROOT = Path(__file__).resolve().parents[2]

@pytest.mark.parametrize('path,required,removed', [
    ('/', ['가는 길에', '내 주문', '실제 주문이나 결제는 되지 않아요.', '할인 후 결제 한도', '얼마나 돌아가도 괜찮나요?'], ['TIME-FIRST COFFEE','독립 제품 데모','시나리오 시드']),
    ('/classic', ['운영 화면','매장에서 주문 받기','주문 수신 확인','출력·알림 요청 기록'], ['BACKEND CONSOLE','Merchant Fulfillment Reliability','Exactly-once effects','JIT 제조 Window']),
    ('/repair-lab', ['취소된 주문, 정산도 맞게 처리됐을까요?','처리할 내역 보기','확인한 내용으로 기록','처리 결과'], ['전표 allocation','모의 조치 원장','REPAIR REVIEW']),
])
def test_each_demo_entry_has_revised_copy(path, required, removed):
    r=TestClient(app).get(path)
    assert r.status_code==200
    for text in required: assert text in r.text, (path,text)
    for text in removed: assert text not in r.text, (path,text)

@pytest.mark.parametrize('path,removed', [
    ('demo/route/product.js',['모의 결제','모의 승인','MY ORDER','SIMULATED RECEIPT','주문은 그대로, 더 나은 매장으로.']),
    ('demo/route/benefits-ui.js',['모형 마감 충족','시드 ${','사용 보류','최종 모의 결제']),
    ('services/ops-console/templates/index.html',['replay 실행','deterministic correctness','aggregate: ']),
])
def test_dynamic_and_secondary_copy_is_reviewed(path,removed):
    text=(ROOT/path).read_text()
    for old in removed: assert old not in text,(path,old)


def test_api_codes_and_numeric_evidence_are_not_rewritten_as_ui_copy():
    client=TestClient(app)
    s=client.post('/api/route/journeys',json={'coupon_id':'welcome500','points':1000}).json()
    wave=next(p for p in s['recommendations'] if p['store_id']=='wave')
    assert wave['pricing']['coupon_reason']=='APPLIED'
    assert wave['price']==2800
    r=client.post(f"/api/route/journeys/{s['id']}/commands",json={
        'action':'reserve','expected_version':s['version'],'request_id':'copy-contract','quote_id':'missing'})
    assert r.status_code==409 and r.json()['detail']['code']=='STALE_QUOTE'
