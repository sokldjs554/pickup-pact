#!/usr/bin/env python3
"""Record the reviewed public release: refusal, automatic recovery, one receipt.

No intercepted responses or edited DOM. Virtual manufacture-clock steps and
between-scene waits are cut; the recorded scene clips play at normal speed.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import time
from urllib.request import urlopen
from playwright.sync_api import sync_playwright, expect
from capture_readme_demo import edit


def get_json(base, path, expected=None):
    with urlopen(base + path, timeout=40) as response:
        value = json.load(response)
    if expected is not None:
        assert value.get('release_commit') == expected, value
    return value


def capture(base: str, expected: str, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    raw = output / 'raw'; raw.mkdir(exist_ok=True)
    before = get_json(base, '/health', expected)
    runtime = get_json(base, '/api/route/runtime')
    assert runtime['automatic_recovery'] and runtime['merchant_transport'] == 'http', runtime
    scenes, shots, errors, requests = [], [], [], []
    contexts = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(viewport={'width':1280, 'height':900},
                record_video_dir=str(raw), record_video_size={'width':1280, 'height':900})
            contexts.append(context)
            t0 = time.monotonic(); page = context.new_page()
            page.on('pageerror', lambda err: errors.append(str(err)))
            page.on('request', lambda req: requests.append({'method':req.method,'url':req.url,
                'at':round(time.monotonic()-t0,3),
                'action':(req.post_data_json or {}).get('action') if req.method == 'POST' and req.url.startswith(base+'/api/') else None}))
            def shot(name):
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
                page.screenshot(path=str(output/(name+'.png')), full_page=False)
                shots.append(name+'.png')
            @contextmanager
            def scene(name, caption, seconds):
                start = time.monotonic()-t0
                yield
                page.wait_for_timeout(max(1.5, seconds-(time.monotonic()-t0-start))*1000)
                scenes.append({'name':name, 'caption':caption, 'start':start,
                    'duration':time.monotonic()-t0-start})
                shot(name)
            def data():
                sid=page.evaluate("localStorage.getItem('pickup-pact.route-journey')")
                response=page.request.get(base+'/api/route/journeys/'+sid)
                assert response.ok, response.text()
                return response.json()
            def idle():
                page.wait_for_function("!document.body.classList.contains('busy')")
            def fault(value):
                page.locator('#handoffFault').select_option(value)
                page.locator('#setHandoffFault').click(); idle()
            def choose_oat():
                page.locator('.route-card[data-hover=oat] [data-quote]').click()
                expect(page.locator('#confirmDialog')).to_be_visible()
            page.goto(base+'/',wait_until='networkidle',timeout=60000)
            expect(page.locator('#map circle')).not_to_have_count(0)
            with scene('01-home','매장이 거절해도, 원래 주문은 남아요',3):
                expect(page.locator('h1').first).to_contain_text('커피 한 잔')
            with scene('02-order','전 매장 쿠폰과 1,000P를 적용해 주문해요',5):
                page.locator('#coupon').select_option('welcome500')
                page.locator('#points').fill('1000')
                page.locator('#findRoutes').click()
                wave=page.locator('.route-card[data-hover=wave]')
                expect(wave).to_contain_text('2,800원'); wave.scroll_into_view_if_needed()
            wave.locator('[data-quote]').click(); idle()
            page.locator('#orderArea [data-action=busy]').click(); idle()
            original=data(); oid=original['order']['id']
            assert original['order']['store_id']=='wave' and original['wallet']['held_points']==1000
            fault('target_reject'); choose_oat()
            with scene('03-refused','새 매장이 거절하면, 원래 주문과 혜택을 유지해요',6):
                page.locator('#confirmTransfer').click()
                expect(page.locator('#handoffMessage')).to_contain_text('원래 주문과 혜택은 그대로')
                page.locator('#handoffPanel').scroll_into_view_if_needed()
            refused=data()
            assert refused['order']==original['order'] and refused['wallet']==original['wallet']
            fault('after_target_hold'); choose_oat()
            assert not page.locator('#couponLossWarning').is_visible()
            with scene('04-automatic-recovery','응답이 끊겨도, 서버가 같은 작업을 다시 확인해요',7):
                page.locator('#confirmTransfer').click()
                expect(page.locator('#recoverHandoff')).to_be_visible()
                pending=data(); assert pending['handoff_pending']
                page.locator('#handoffPanel').scroll_into_view_if_needed(); shot('04-pending')
                # Deliberately no recover command and no server mutation via GET.
                expect(page.locator('#handoffMessage')).to_contain_text('같은 주문으로 매장을 바꿨어요',timeout=15000)
            moved=data()
            assert moved['order']['id']==oid and moved['order']['store_id']=='oat'
            assert moved['receipt']['authorization_count']==1 and moved['receipt']['capture_count']==0
            with scene('05-recovered','복구 후에도 같은 주문 · 쿠폰과 포인트 그대로',5):
                page.locator('#handoffPanel').scroll_into_view_if_needed()
                expect(page.locator('#handoffPanel')).to_contain_text(oid)
            page.locator('nav [data-view=merchant]').click()
            advance=page.locator('#merchantContent [data-action=advance]')
            if advance.count(): advance.click(); idle()
            start=page.locator('#merchantContent [data-action=start]'); expect(start).to_be_enabled(); start.click(); idle()
            page.locator('#merchantContent [data-action=advance]').click(); idle()
            ready=page.locator('#merchantContent [data-action=ready]'); expect(ready).to_be_enabled(); ready.click(); idle()
            shot('06-merchant')
            page.locator('nav [data-view=customer]').click()
            with scene('07-pickup','체험 시계를 진행해 수령 · 결제는 한 번만',5):
                page.locator('#claimCode').fill(page.locator('#pickupCode').inner_text().strip())
                page.locator('#claim').click()
                expect(page.locator('#orderArea .status-pill')).to_have_text('수령 완료')
                page.locator('#benefitsEarned').scroll_into_view_if_needed()
            final=data()
            assert final['order']['id']==oid and final['receipt']['capture_count']==1
            assert final['receipt']['net_paid']==3200 and final['wallet']['spent']==1000
            assert final['wallet']['earned']==32 and final['wallet']['available_points']==1032
            with scene('08-receipt','같은 주문의 영수증 · 3,200원 / 1,000P 사용·32P 적립',5):
                page.locator('nav [data-view=receipt]').click()
                expect(page.locator('#receiptContent')).to_contain_text(oid)
                page.locator('#receiptContent').scroll_into_view_if_needed()
            page.locator('nav [data-view=customer]').click()
            with page.expect_response(lambda r:r.url.endswith('/api/route/transfer-comparison') and r.request.method=='POST',timeout=30000) as response_event:
                page.locator('#compareHandoff').click()
            response=response_event.value; assert response.status==200
            comparison=response.json(); assert len(comparison['cases'])==6
            expect(page.locator('#handoffComparisonResult tbody tr')).to_have_count(6)
            with scene('09-comparison','취소 후 재주문과 비교 · 같았던 결과도 함께 공개해요',5):
                page.locator('#handoffComparison').scroll_into_view_if_needed()
            assert not any(r['action']=='recover' for r in requests), requests
            video=page.video; context.close(); contexts.remove(context); assert video is not None
            source=Path(video.path())
            states={'original':original,'refused':refused,'pending':pending,'recovered':moved,'final':final}
            (output/'journey-states.json').write_text(json.dumps(states,ensure_ascii=False,indent=2))
            (output/'transfer-comparison.json').write_text(json.dumps(comparison,ensure_ascii=False,indent=2))
            # Mobile is another real, isolated session, not a resized desktop shot.
            mobile=browser.new_context(viewport={'width':390,'height':844}); contexts.append(mobile)
            mp=mobile.new_page(); mp.on('pageerror',lambda e:errors.append(str(e)))
            mp.goto(base+'/',wait_until='networkidle',timeout=60000)
            mp.screenshot(path=str(output/'mobile-home.png'),full_page=True)
            mp.locator('#coupon').select_option('welcome500'); mp.locator('#points').fill('1000')
            mp.locator('#findRoutes').click(); mp.locator('.route-card[data-hover=wave] [data-quote]').click()
            mp.locator('#orderArea [data-action=busy]').click()
            mp.locator('#handoffFault').select_option('after_target_hold'); mp.locator('#setHandoffFault').click()
            mp.wait_for_function("!document.body.classList.contains('busy')")
            mp.locator('.route-card[data-hover=oat] [data-quote]').click(); mp.locator('#confirmTransfer').click()
            expect(mp.locator('#recoverHandoff')).to_be_visible()
            mp.locator('#handoffPanel').scroll_into_view_if_needed()
            mp.screenshot(path=str(output/'mobile-pending.png'),full_page=False)
            expect(mp.locator('#handoffMessage')).to_contain_text('같은 주문으로 매장을 바꿨어요',timeout=15000)
            mp.screenshot(path=str(output/'mobile-recovered.png'),full_page=False)
            assert mp.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
            mobile.close(); contexts.remove(mobile)
            after=get_json(base,'/health',expected)
            assert not errors,errors
            report={'base_url':base,'recorded_release_commit':expected,'health_before':before,'health_after':after,
                'runtime':runtime,'browser':browser.version,'viewport':{'width':1280,'height':900},
                'source_video':str(source.resolve().relative_to(output.resolve())),
                'scenes':scenes,'screenshots':shots,'order_id':oid,'same_order':True,
                'refusal_preserved_order_and_benefits':True,'recovery_without_manual_command':True,
                'cash_due':3200,'capture_count':1,'points_spent':1000,'points_earned':32,'wallet_available':1032,
                'page_errors':errors,'api_requests':requests,'comparison_semantic_sha256':comparison['semantic_sha256'],
                'editing':'Actual public Chromium recording. Normal-speed scene clips; between-scene waiting and virtual preparation steps omitted. Independent synthetic merchants, simulated payments.'}
            (output/'capture.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
            return report
        except Exception as error:
            (output/'capture-failure.json').write_text(json.dumps({'error':str(error),'scenes':scenes,'page_errors':errors,'requests':requests},ensure_ascii=False,indent=2))
            raise
        finally:
            for ctx in contexts: ctx.close()
            browser.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url',default='https://pickup-pact-demo.onrender.com')
    parser.add_argument('--expected-commit',required=True)
    parser.add_argument('--output',type=Path,default=Path('verification/recovery-media'))
    args=parser.parse_args()
    report=capture(args.base_url.rstrip('/'),args.expected_commit,args.output)
    edit(args.output,report)
    print(json.dumps({key:report[key] for key in ('recorded_release_commit','edited_duration_seconds','same_order','recovery_without_manual_command','page_errors')},ensure_ascii=False))
