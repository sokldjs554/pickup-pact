#!/usr/bin/env python3
"""Real HTTP browser check. No intercepted requests or static fixture fallback.

A navigation policy block records failure; it must not be counted as passing.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
from urllib.request import urlopen
from playwright.sync_api import sync_playwright,expect


def verify(base,output,repeat,expected_commit=None):
    output.mkdir(parents=True,exist_ok=True);runs=[]
    with urlopen(base+'/health',timeout=20) as r:health=json.load(r)
    if expected_commit:assert health.get('release_commit')==expected_commit,health
    with urlopen(base+'/api/route/runtime',timeout=20) as r:runtime=json.load(r)
    automatic=runtime['automatic_recovery']
    with sync_playwright() as p:
        options={'headless':True}
        if os.environ.get('ROUTE_BROWSER_PATH'):options['executable_path']=os.environ['ROUTE_BROWSER_PATH']
        browser=p.chromium.launch(**options)
        try:
            for repetition in range(1,repeat+1):
                for mode,width,height in [('desktop',1440,1000),('mobile',390,844)]:
                    kwargs={'viewport':{'width':width,'height':height}}
                    if repetition==1 and mode=='desktop':
                        kwargs.update(record_video_dir=str(output/'recording'),record_video_size={'width':1280,'height':900})
                    context=browser.new_context(**kwargs);page=context.new_page();errors=[]
                    page.on('pageerror',lambda err:errors.append(str(err)))
                    def snapshot():
                        sid=page.evaluate("localStorage.getItem('pickup-pact.route-journey')")
                        return page.request.get(base+'/api/route/journeys/'+sid).json()
                    def shot(name):
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                        if repetition==1:page.screenshot(path=str(output/f'{mode}-{name}.png'),full_page=True)
                    def select_oat():
                        page.locator('.route-card[data-hover="oat"] [data-quote]').click()
                        expect(page.locator('#confirmDialog')).to_be_visible();page.locator('#confirmTransfer').click()
                    def fault(value):
                        page.locator('#handoffFault').select_option(value);page.locator('#setHandoffFault').click()
                        expect(page.locator('#setHandoffFault')).to_be_enabled()
                        page.wait_for_function("!document.body.classList.contains('busy')")
                    page.goto(base+'/',wait_until='networkidle');shot('01-home')
                    page.locator('#coupon').select_option('welcome500');page.locator('#points').fill('1000')
                    page.locator('#findRoutes').click();expect(page.locator('[data-quote]')).not_to_have_count(0)
                    page.locator('.route-card[data-hover="wave"] [data-quote]').click()
                    expect(page.locator('#handoffPanel')).to_be_visible()
                    page.locator('#orderArea [data-action=busy]').click()
                    expect(page.locator('#orderArea .rescue-banner')).to_be_visible()
                    original=snapshot();oid=original['order']['id']
                    fault('target_reject');select_oat()
                    expect(page.locator('#handoffMessage')).to_contain_text('원래 주문과 혜택은 그대로')
                    rejected=snapshot();assert rejected['order']==original['order'] and rejected['wallet']==original['wallet'];shot('02-rejected')
                    fault('after_target_hold');select_oat()
                    expect(page.locator('#recoverHandoff')).to_be_visible()
                    expect(page.locator('#orderArea .status-pill')).to_have_text('처리 확인 중')
                    assert snapshot()['handoff_pending'];shot('03-pending')
                    page.reload(wait_until='networkidle')
                    if automatic:
                        # No recover POST. The worker completes even without this
                        # GET-only page polling. Unit tests also close the page.
                        expect(page.locator('#handoffMessage')).to_contain_text('같은 주문으로 매장을 바꿨어요',timeout=15000)
                    else:
                        expect(page.locator('#recoverHandoff')).to_be_visible()
                        page.locator('#recoverHandoff').click()
                    expect(page.locator('#handoffMessage')).to_contain_text('같은 주문으로 매장을 바꿨어요')
                    moved=snapshot();assert moved['order']['id']==oid and moved['order']['store_id']=='oat'
                    assert moved['receipt']['authorization_count']==1;shot('04-recovered')
                    page.locator('nav [data-view="merchant"]').click()
                    advance=page.locator('#merchantContent [data-action="advance"]')
                    if advance.count():advance.click()
                    expect(page.locator('#merchantContent [data-action="start"]')).to_be_enabled()
                    page.locator('#merchantContent [data-action="start"]').click()
                    page.locator('#merchantContent [data-action="advance"]').click()
                    expect(page.locator('#merchantContent [data-action="ready"]')).to_be_enabled()
                    page.locator('#merchantContent [data-action="ready"]').click()
                    page.locator('nav [data-view="customer"]').click()
                    code=page.locator('#pickupCode').inner_text().strip()
                    page.locator('#claimCode').fill(code);page.locator('#claim').click()
                    expect(page.locator('#orderArea .status-pill')).to_have_text('수령 완료')
                    final=snapshot();assert final['receipt']['capture_count']==1 and final['receipt']['net_paid']==3200
                    assert final['wallet']['spent']==1000 and final['wallet']['earned']==32
                    page.locator('nav [data-view="receipt"]').click();shot('05-receipt')
                    page.locator('nav [data-view="customer"]').click();page.locator('#compareHandoff').click()
                    expect(page.locator('#handoffComparisonResult tbody tr')).to_have_count(6)
                    expect(page.locator('#handoffComparisonResult')).to_contain_text('기존 매장에서 주문 유지');shot('06-comparison')
                    assert errors==[],errors
                    runs.append(dict(repetition=repetition,viewport=mode,result='passed',page_errors=errors,
                        order_id=oid,release_commit=health.get('release_commit'),base_url=base,
                        automatic_recovery=automatic,merchant_transport=runtime['merchant_transport']))
                    (output/'browser-results.json').write_text(json.dumps(runs,ensure_ascii=False,indent=2))
                    context.close()
        except Exception as e:
            (output/'browser-failure.json').write_text(json.dumps(dict(result='failed',error=str(e),completed_runs=runs),ensure_ascii=False,indent=2))
            raise
        finally:browser.close()
    return runs

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--base-url',default='http://127.0.0.1:10000')
    p.add_argument('--output',type=Path,default=Path('verification/handoff-browser'));p.add_argument('--repeat',type=int,default=3)
    p.add_argument('--expected-commit');a=p.parse_args()
    if not 1<=a.repeat<=10:raise SystemExit('repeat must be between 1 and 10')
    print(json.dumps(verify(a.base_url.rstrip('/'),a.output,a.repeat,a.expected_commit),ensure_ascii=False,indent=2))
