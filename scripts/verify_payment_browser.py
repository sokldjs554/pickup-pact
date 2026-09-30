#!/usr/bin/env python3
"""Actual browser acceptance of the integrated independent synthetic PG, not mocks."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import time
from urllib.request import urlopen
from playwright.sync_api import sync_playwright, expect


def verify(base: str, output: Path, repeat=3, expected_commit=None):
    output.mkdir(parents=True,exist_ok=True)
    def read(path):
        with urlopen(base+path,timeout=30) as r:return json.load(r)
    before=read('/health');runtime=read('/api/route/runtime')
    if expected_commit:assert before['release_commit']==expected_commit,before
    assert runtime['payment_transport']=='http' and runtime['payment_storage_ready'] and runtime['automatic_recovery']
    reports=[]
    with sync_playwright() as pw:
        options={'headless':True}
        if os.environ.get('ROUTE_BROWSER_PATH'):options['executable_path']=os.environ['ROUTE_BROWSER_PATH']
        browser=pw.chromium.launch(**options)
        for run in range(1,repeat+1):
            for mode,width,height in [('desktop',1440,1000),('mobile',390,844)]:
                for scenario in ['none','authorize_reply_lost','capture_reply_lost','void_reply_lost','declined']:
                    context=browser.new_context(viewport={'width':width,'height':height},locale='ko-KR',
                        record_video_dir=str(output/'raw') if run==1 and mode=='desktop' and scenario=='authorize_reply_lost' else None)
                    context.set_default_timeout(15000)
                    context.tracing.start(screenshots=True,snapshots=True,sources=True)
                    page=context.new_page();errors=[];commands=[]
                    page.on('pageerror',lambda e:errors.append(str(e)))
                    def record(request):
                        if request.method=='POST' and '/api/route/' in request.url:commands.append({'url':request.url,'body':request.post_data_json})
                    page.on('request',record)
                    def stage(value):expect(page.locator('#guidePanel')).to_have_attribute('data-stage',value,timeout=20000)
                    def idle():page.wait_for_function("!document.body.classList.contains('busy')")
                    def shot(name):
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),name
                        if run==1:page.screenshot(path=str(output/f'{mode}-{scenario}-{name}.png'))
                    def snapshot():
                        sid=page.evaluate("localStorage.getItem('pickup-pact.route-journey')")
                        r=context.request.get(base+'/api/route/journeys/'+sid);assert r.ok,r.text();return r.json()
                    def action(next_stage):page.locator('#guideAction').click();stage(next_stage);idle()
                    try:
                        url=base+'/' + ('?payment='+scenario if scenario!='declined' else '')
                        page.goto(url,wait_until='networkidle');expect(page.locator('#quickStart')).to_be_enabled()
                        assert page.locator('#customizeOrder').get_attribute('open') is None
                        if scenario=='declined':
                            page.locator('#paymentEntryOptions summary').click();page.locator('#paymentCard').select_option('demo-declined')
                        shot('home')
                        # Native keyboard activation keeps the default entry accessible.
                        page.locator('#quickStart').focus();page.keyboard.press('Enter')
                        if scenario=='authorize_reply_lost':
                            stage('waiting');pending=snapshot();assert pending['order'] is None and pending['pending_order_id']
                            assert pending['payment']['state']=='CONFIRMING_APPROVAL'
                            expect(page.locator('#guideAction')).to_be_disabled();shot('approval-unknown')
                            sid=pending['id'];oid=pending['pending_order_id']
                            page.close()  # only the server worker can advance while the page is absent
                            until=time.monotonic()+15
                            while True:
                                r=context.request.get(base+'/api/route/journeys/'+sid);assert r.ok
                                saved=r.json()
                                if not saved['handoff_pending']:break
                                assert time.monotonic()<until,saved
                                time.sleep(.2)
                            assert saved['order']['id']==oid
                            page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)));page.on('request',record)
                            page.goto(base+'/',wait_until='networkidle');stage('busy')
                        elif scenario=='declined':
                            stage('declined');idle();s=snapshot();assert s['order'] is None and s['wallet']['held_points']==0
                            page.locator('#paymentControls summary').click();page.locator('#retryApprovedCard').click();stage('busy')
                        else:stage('busy')
                        idle();original=snapshot();oid=original['order']['id']
                        if scenario=='void_reply_lost':
                            page.locator('#orderArea [data-action=cancel]').click();stage('waiting')
                            assert snapshot()['order']['state']=='RESERVED'
                            stage('cancelled');idle();action('cancelled')
                            expected_cash=0
                        else:
                            action('reject');page.locator('#guideAction').click()
                            expect(page.locator('#confirmDialog')).to_be_visible();page.locator('#confirmTransfer').click();stage('disconnect');idle()
                            rejected=snapshot();assert rejected['order']==original['order'] and rejected['wallet']==original['wallet']
                            page.locator('#guideAction').click();expect(page.locator('#confirmDialog')).to_be_visible()
                            page.locator('#confirmTransfer').click();stage('waiting');stage('prepare');idle();shot('recovered')
                            action('ready');expect(page.locator('#merchantTerminals')).to_be_visible()
                            expect(page.locator('#merchantTerminals [data-shop=oat]')).to_contain_text('제조 중')
                            shot('terminals');action('claim');page.locator('#guideAction').click()
                            if scenario=='capture_reply_lost':
                                stage('waiting');s=snapshot();assert s['order']['state']=='READY' and s['wallet']['spent']==0
                                assert s['payment']['state']=='CONFIRMING_CAPTURE';shot('capture-unknown')
                            stage('receipt');idle();action('receipt');expected_cash=3200
                        expect(page.locator('#receiptView')).to_be_visible()
                        expect(page.locator('#finalProof')).to_have_attribute('data-status','MATCH',timeout=20000)
                        final=snapshot();proof=context.request.get(base+f"/api/route/journeys/{final['id']}/reconciliation").json()
                        assert proof['status']=='MATCH' and proof['terminal'] and proof['observation']['stable'],proof
                        assert final['order']['id']==oid and proof['payment']['captured_krw']==expected_cash
                        assert proof['payment']['held_krw']==0 and proof['payment']['capture_count']==int(expected_cash>0)
                        assert proof['benefits']['spent']==(1000 if expected_cash else 0)
                        assert proof['benefits']['earned']==(32 if expected_cash else 0)
                        assert not any(c['body'].get('action')=='recover' for c in commands)
                        assert not errors,errors
                        page.locator('#finalProof').scroll_into_view_if_needed();shot('result')
                        record_data={'result':'passed','repetition':run,'viewport':mode,'scenario':scenario,'order_id':oid,
                            'release_commit':before['release_commit'],'base_url':base,'proof':proof,'page_errors':errors,'commands':commands}
                        reports.append(record_data)
                        (output/'results.json').write_text(json.dumps(reports,ensure_ascii=False,indent=2))
                    except Exception as error:
                        if not page.is_closed():page.screenshot(path=str(output/f'failure-{run}-{mode}-{scenario}.png'),full_page=True)
                        (output/'failure.json').write_text(json.dumps({'error':str(error),'scenario':scenario,'viewport':mode,'page_errors':errors,'completed':len(reports),'commands':commands},ensure_ascii=False,indent=2))
                        raise
                    finally:
                        context.tracing.stop(path=str(output/f'trace-{run}-{mode}-{scenario}.zip'));context.close()
        browser.close()
    assert read('/health')['release_commit']==before['release_commit']
    return {'count':len(reports),'passed':all(r['result']=='passed' for r in reports),'release_commit':before['release_commit']}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--base-url',default='http://127.0.0.1:10000')
    p.add_argument('--output',type=Path,default=Path('verification/payment-browser'));p.add_argument('--repeat',type=int,default=3);p.add_argument('--expected-commit')
    a=p.parse_args()
    if not 1<=a.repeat<=5:p.error('repeat must be from 1 to 5')
    print(json.dumps(verify(a.base_url.rstrip('/'),a.output,a.repeat,a.expected_commit),ensure_ascii=False))
