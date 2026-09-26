#!/usr/bin/env python3
"""Real browser proof of participation, terms, fair baseline and funding.

No route interception, DOM substitution or fabricated server state. Captures
belong to the checked release. This script does not edit the demo video.
"""
import argparse,json,os,time
from pathlib import Path
from urllib.request import urlopen
from playwright.sync_api import sync_playwright,expect


def verify(base,output,repeat=3,expected=None):
    output.mkdir(parents=True,exist_ok=True);runs=[]
    with urlopen(base+'/health',timeout=40) as r:before=json.load(r)
    if expected:assert before['release_commit']==expected,before
    with sync_playwright() as pw:
        opts={'headless':True}
        if os.environ.get('ROUTE_BROWSER_PATH'):opts['executable_path']=os.environ['ROUTE_BROWSER_PATH']
        browser=pw.chromium.launch(**opts)
        ctx=None;page=None
        try:
            for n in range(1,repeat+1):
                for label,w,h in [('desktop',1280,960),('mobile',390,844)]:
                    ctx=browser.new_context(viewport={'width':w,'height':h})
                    page=ctx.new_page();errors=[];requests=[]
                    page.on('pageerror',lambda err:errors.append(str(err)))
                    page.on('request',lambda r:requests.append(r.post_data_json) if r.method=='POST' and '/commands' in r.url else None)
                    def idle():page.wait_for_function("!document.body.classList.contains('busy')")
                    def shot(name):
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                        if n==1:page.screenshot(path=str(output/f'{label}-{name}.png'),full_page=False)
                    def state():
                        sid=page.evaluate("localStorage.getItem('pickup-pact.route-journey')")
                        response=page.request.get(base+'/api/route/journeys/'+sid)
                        assert response.ok,response.text();return response.json()
                    def fault(value):
                        idle();page.locator('#handoffFault').select_option(value)
                        with page.expect_response(lambda r:r.url.endswith('/transfer-controls') and r.request.method=='POST') as re:
                            page.locator('#setHandoffFault').click()
                        assert re.value.json()['next_transfer_fault']==value
                        idle()
                    def consent_frame():
                        geometry=page.evaluate("""() => {
                            const dialog=document.getElementById('confirmDialog').getBoundingClientRect();
                            const inside=id=>{const r=document.getElementById(id).getBoundingClientRect();
                                return r.top>=Math.max(0,dialog.top) && r.bottom<=Math.min(innerHeight,dialog.bottom)
                                    && r.left>=dialog.left && r.right<=dialog.right;};
                            return {title:inside('dialogTitle'),button:inside('confirmTransfer')};
                        }""")
                        assert geometry['title'] and geometry['button'],geometry
                    def choose():
                        idle();page.locator('.route-card[data-hover=oat] [data-quote]').click()
                        expect(page.locator('#transferAgreement')).to_contain_text('360mL')
                        expect(page.locator('#transferAgreement')).to_contain_text('맛은 매장마다')
                        expect(page.locator('#dialogTitle')).to_be_focused()
                        assert page.locator('#dialogBody').evaluate('(el)=>el.scrollTop')==0
                        consent_frame()
                    page.goto(base+'/',wait_until='networkidle',timeout=60000)
                    expect(page.locator('h1').first).to_contain_text('매장 변경이 막혀도');shot('01-home')
                    page.locator('#coupon').select_option('welcome500');page.locator('#points').fill('1000')
                    page.locator('#findRoutes').click();idle()
                    page.locator('.route-card[data-hover=wave] [data-quote]').click();idle()
                    original=state();oid=original['order']['id']
                    # Actual refresh must not discard an unsubmitted selection.
                    page.locator('#handoffFault').select_option('target_reject')
                    page.locator('#orderArea [data-action=refresh]').click();idle()
                    expect(page.locator('#handoffFault')).to_have_value('target_reject')
                    page.locator('#orderArea [data-action=busy]').click();idle()
                    fault('target_reject');choose();shot('02-terms')
                    page.locator('#transferAgreement .agreement-cost summary').click()
                    page.locator('#transferAgreement .agreement-cost').scroll_into_view_if_needed()
                    consent_frame();shot('03-funding-preview')
                    page.locator('#confirmTransfer').click();idle()
                    expect(page.locator('#handoffMessage')).to_contain_text('원래 주문과 혜택')
                    refused=state();assert refused['order']==original['order'] and refused['wallet']==original['wallet']
                    fault('after_target_hold');choose();page.locator('#confirmTransfer').click()
                    expect(page.locator('#handoffMessage')).to_contain_text('자동으로',timeout=5000)
                    expect(page.locator('#notice')).to_contain_text('자동')
                    expect(page.locator('#notice')).not_to_have_class('error')
                    expect(page.locator('#handoffMessage')).to_contain_text('같은 주문으로 매장을 바꿨어요',timeout=15000)
                    expect(page.locator('#notice')).to_contain_text('같은 주문으로 매장을 바꿨어요')
                    expect(page.locator('#notice')).not_to_have_class('error')
                    moved=state();assert moved['order']['id']==oid
                    assert moved['order']['commercial_terms']['funding']['customer_cash']==3200
                    page.locator('#handoffPanel').scroll_into_view_if_needed();shot('04-recovered')
                    page.locator('nav [data-view=merchant]').click()
                    advance=page.locator('#merchantContent [data-action=advance]')
                    if advance.count():advance.click();idle()
                    page.locator('#merchantContent [data-action=start]').click();idle()
                    page.locator('#merchantContent [data-action=advance]').click();idle()
                    page.locator('#merchantContent [data-action=ready]').click();idle()
                    page.locator('nav [data-view=customer]').click()
                    page.locator('#claimCode').fill(page.locator('#pickupCode').inner_text().strip())
                    page.locator('#claim').click();idle()
                    final=state();m=final['receipt']['settlement']
                    assert final['receipt']['capture_count']==1
                    assert m['customer_cash']==3200 and m['platform_coupon']==500 and m['platform_points']==1000 and m['merchant_receivable']==4700
                    page.locator('nav [data-view=receipt]').click()
                    page.locator('#settlementBreakdown summary').click()
                    page.locator('#settlementBreakdown').scroll_into_view_if_needed();shot('05-settlement')
                    page.locator('nav [data-view=customer]').click()
                    began=time.monotonic()
                    with page.expect_response(lambda r:r.url.endswith('/transfer-comparison'),timeout=30000) as re:
                        page.locator('#compareHandoff').click()
                    assert re.value.status==200,re.value.text();report=re.value.json()
                    assert report['policies']==['stay','cancel_reorder','reserve_first_reorder','guarded_transfer']
                    for scenario in ['reject_after_quote','last_place_taken']:
                        row=next(r for r in report['cases'] if r['scenario']==scenario)
                        assert row['reserve_first_reorder']['original_preserved'] and row['guarded_transfer']['original_preserved']
                    expect(page.locator('#handoffComparisonResult thead')).to_contain_text('새 자리 확보 후 재주문')
                    expect(page.locator('#handoffComparisonResult tbody tr')).to_have_count(6)
                    page.locator('#handoffComparison').scroll_into_view_if_needed();shot('06-fair-comparison')
                    assert not errors,errors
                    assert not any(x.get('action')=='recover' for x in requests)
                    runs.append(dict(pass_number=n,viewport=label,result='passed',release_commit=before['release_commit'],
                        base_url=base,page_errors=errors,same_order=oid,unsubmitted_selection_preserved=True,
                        funding=m,policies=report['policies'],consent_title_and_buttons_pinned=True,operation_notice_tracks_recovery=True,
                        comparison_seconds=time.monotonic()-began))
                    (output/'results.json').write_text(json.dumps(runs,ensure_ascii=False,indent=2)+'\n')
                    ctx.close();ctx=None
        except Exception as error:
            if page is not None and not page.is_closed():page.screenshot(path=str(output/'failure.png'),full_page=True)
            (output/'failure.json').write_text(json.dumps(dict(error=str(error),completed=runs),ensure_ascii=False,indent=2))
            raise
        finally:
            if ctx:ctx.close()
            browser.close()
    with urlopen(base+'/health',timeout=40) as r:after=json.load(r)
    if expected:assert after['release_commit']==expected,after
    return runs

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base-url',default='http://127.0.0.1:10000');p.add_argument('--output',type=Path,default=Path('verification/decision-browser'))
    p.add_argument('--repeat',type=int,default=3);p.add_argument('--expected-commit')
    a=p.parse_args()
    if not 1<=a.repeat<=10:raise SystemExit('repeat must be 1..10')
    print(json.dumps(verify(a.base_url.rstrip('/'),a.output,a.repeat,a.expected_commit),ensure_ascii=False,indent=2))
