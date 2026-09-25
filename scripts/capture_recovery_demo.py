#!/usr/bin/env python3
"""Record the exact deployed recovery journey; do not fabricate UI or results."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import time
from urllib.request import urlopen
from playwright.sync_api import sync_playwright, expect


def record(base: str, expected: str, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    def health():
        with urlopen(base+'/health', timeout=30) as response:
            result=json.load(response)
        assert result['release_commit']==expected, result
        return result
    before=health()
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        context=browser.new_context(viewport={'width':1280,'height':900},
            record_video_dir=str(output/'raw'),record_video_size={'width':1280,'height':900})
        page=context.new_page(); errors=[]; scenes=[]; recover_posts=[]
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('request', lambda req: recover_posts.append(req.post_data) if req.method=='POST' and req.post_data and '"action":"recover"' in req.post_data.replace(' ','') else None)
        started=time.monotonic()
        def state():
            sid=page.evaluate("localStorage.getItem('pickup-pact.route-journey')")
            result=page.request.get(base+'/api/route/journeys/'+sid)
            assert result.ok
            return result.json()
        def scene(name, caption, focus=None, hold=3.5):
            if focus: page.locator(focus).scroll_into_view_if_needed()
            page.wait_for_timeout(200)
            start=time.monotonic()-started
            page.wait_for_timeout(hold*1000)
            end=time.monotonic()-started
            page.screenshot(path=str(output/(name+'.png')),full_page=True)
            if focus: page.locator(focus).screenshot(path=str(output/(name+'-panel.png')))
            scenes.append({'name':name,'caption':caption,'start':start,'end':end})
        def fault(value):
            page.locator('#handoffFault').select_option(value)
            page.locator('#setHandoffFault').click()
            page.wait_for_function("!document.body.classList.contains('busy')")
        def transfer():
            page.locator('.route-card[data-hover="oat"] [data-quote]').click()
            expect(page.locator('#confirmDialog')).to_be_visible()
            page.locator('#confirmTransfer').click()
        page.goto(base+'/',wait_until='networkidle')
        response=page.request.get(base+'/api/route/runtime');runtime=response.json()
        assert runtime['merchant_transport']=='http' and runtime['automatic_recovery'] is True, runtime
        scene('01-home','주문을 바꾸다 문제가 생기면, 원래 주문은 남을까요?',hold=4)
        page.locator('#coupon').select_option('welcome500')
        page.locator('#points').fill('1000')
        page.locator('#findRoutes').click()
        expect(page.locator('.route-card[data-hover="wave"] [data-quote]')).to_be_visible()
        page.locator('.route-card[data-hover="wave"] [data-quote]').click()
        expect(page.locator('#handoffPanel')).to_be_visible()
        original=state(); oid=original['order']['id']
        assert original['order']['price']==2800 and original['wallet']['held_points']==1000
        scene('02-order','500원 쿠폰과 1,000P로 주문했어요. 변경 전 결제 예정액 2,800원.', '#orderArea',4)
        page.locator('#orderArea [data-action="busy"]').click()
        expect(page.locator('#orderArea .rescue-banner')).to_be_visible()
        fault('target_reject'); transfer()
        expect(page.locator('#handoffMessage')).to_contain_text('원래 주문과 혜택은 그대로')
        rejected=state()
        assert rejected['order']==original['order'] and rejected['wallet']==original['wallet']
        scene('03-rejected','새 매장이 거절해도, 원래 주문과 적용한 혜택은 그대로예요.', '#handoffPanel',5)
        fault('after_target_hold')
        page.locator('.route-card[data-hover="oat"] [data-quote]').click()
        expect(page.locator('#confirmDialog')).to_be_visible()
        scene('04-consent','메뉴 차액 400원을 확인하고 바꿔요. 전 매장 쿠폰과 포인트는 유지돼요.',hold=5)
        page.locator('#confirmTransfer').click()
        expect(page.locator('#orderArea .status-pill')).to_have_text('처리 확인 중')
        pending=state();assert pending['handoff_pending']
        page.locator('#handoffPanel').scroll_into_view_if_needed()
        start=time.monotonic()-started
        page.locator('#handoffPanel').screenshot(path=str(output/'05-pending-panel.png'))
        # Keep the real wait in the recording; do not freeze or accelerate it.
        expect(page.locator('#handoffMessage')).to_contain_text('같은 주문으로 매장을 바꿨어요',timeout=20000)
        end=time.monotonic()-started
        scenes.append({'name':'05-auto-recovery','caption':'매장 답이 끊겼어요. 다시 누르지 않아도 서버가 처리 결과를 확인해요.','start':start,'end':end})
        moved=state()
        assert moved['order']['id']==oid and moved['order']['store_id']=='oat'
        assert moved['order']['price']==3200 and moved['wallet']['held_points']==1000
        scene('06-recovered','같은 주문으로 복구됐어요. 두 매장에서 중복 제조하지 않아요.','#handoffPanel',5)
        page.locator('nav [data-view="merchant"]').click()
        advance=page.locator('#merchantContent [data-action="advance"]')
        if advance.count(): advance.click()
        expect(page.locator('#merchantContent [data-action="start"]')).to_be_enabled()
        page.locator('#merchantContent [data-action="start"]').click()
        page.locator('#merchantContent [data-action="advance"]').click()
        expect(page.locator('#merchantContent [data-action="ready"]')).to_be_enabled()
        page.locator('#merchantContent [data-action="ready"]').click()
        page.locator('nav [data-view="customer"]').click()
        code=page.locator('#pickupCode').inner_text().strip()
        page.locator('#claimCode').fill(code);page.locator('#claim').click()
        expect(page.locator('#orderArea .status-pill')).to_have_text('수령 완료')
        page.locator('nav [data-view="receipt"]').click()
        final=state()
        assert final['receipt']['capture_count']==1 and final['receipt']['net_paid']==3200
        assert final['wallet']['spent']==1000 and final['wallet']['earned']==32
        assert final['order']['id']==oid and not recover_posts
        scene('07-receipt','수령까지 주문은 하나. 체험 결제 3,200원과 포인트 사용·적립도 한 번.','#receiptContent',5)
        page.locator('nav [data-view="customer"]').click()
        compare_started=time.monotonic()
        try:
            with page.expect_response(lambda r: r.url.endswith('/api/route/transfer-comparison'), timeout=30000) as response_info:
                page.locator('#compareHandoff').click()
            response=response_info.value
            payload=response.json()
            (output/'comparison-response.json').write_text(json.dumps({
                'status':response.status,'elapsed_seconds':time.monotonic()-compare_started,
                'response':payload},ensure_ascii=False,indent=2)+'\n')
            assert response.status==200, (response.status,payload)
            expect(page.locator('#handoffComparisonResult tbody tr')).to_have_count(6)
        except Exception:
            page.screenshot(path=str(output/'comparison-failure.png'),full_page=True)
            (output/'comparison-failure.html').write_text(page.content())
            context.close();browser.close()
            raise
        scene('08-comparison','취소 후 재주문과 같은 조건으로 비교해요. 같거나 실패한 결과도 남겼어요.','#handoffComparisonResult',5)
        assert errors==[]
        after=health()
        video=page.video
        context.close()
        raw=Path(video.path())
        browser.close()
    report={'recorded_release_commit':expected,'health_before':before,'health_after':after,
        'runtime':runtime,'page_errors':errors,'scenes':scenes,'raw_video':str(raw.relative_to(output)),
        'raw_sha256':hashlib.sha256(raw.read_bytes()).hexdigest(),'same_order':True,
        'order_id':oid,'automatic_recovery_without_customer_recover_post':not recover_posts,
        'cash_due':3200,'capture_count':1,'points_spent':1000,'points_earned':32,
        'disclosure':'공개 합성 데모 실제 브라우저 촬영. 실제 주문·금융 거래 아님. 장면은 정상 속도. 제조용 가상 시계와 장면 사이 조작 일부는 편집에서 제외.'}
    (output/'capture.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    for name,s in [('order',original),('rejected',rejected),('pending',pending),('recovered',moved),('receipt',final)]:
        (output/(name+'.json')).write_text(json.dumps(s,ensure_ascii=False,indent=2)+'\n')
    return report

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--base-url',default='https://pickup-pact-demo.onrender.com')
    p.add_argument('--expected-commit',required=True);p.add_argument('--output',type=Path,default=Path('verification/recovery-media'))
    a=p.parse_args();print(json.dumps(record(a.base_url.rstrip('/'),a.expected_commit,a.output),ensure_ascii=False,indent=2))
