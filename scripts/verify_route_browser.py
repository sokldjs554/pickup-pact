#!/usr/bin/env python3
"""Real HTTP customer -> rescue -> merchant -> pickup -> receipt, desktop/mobile.

No intercepted requests or synthetic responses. Only server-derived state is read.
"""
import argparse
import json
import os
from pathlib import Path
import time
from urllib.request import urlopen
from playwright.sync_api import sync_playwright, expect


def snapshot(page,base):
    sid=page.evaluate("localStorage.getItem('pickup-pact.route-journey')")
    return page.request.get(base+'/api/route/journeys/'+sid).json()


def verify(base,output,repeat,expected_commit=None):
    output.mkdir(parents=True,exist_ok=True); results=[]
    with urlopen(base+'/health',timeout=20) as r: health=json.load(r)
    if expected_commit: assert health['release_commit']==expected_commit,health
    with sync_playwright() as p:
        opts=dict(headless=True)
        if os.environ.get('ROUTE_BROWSER_PATH'): opts['executable_path']=os.environ['ROUTE_BROWSER_PATH']
        browser=p.chromium.launch(**opts)
        for run in range(1,repeat+1):
            for mode,width,height in [('desktop',1440,1000),('mobile',390,844)]:
                context=browser.new_context(viewport=dict(width=width,height=height))
                page=context.new_page(); errors=[]; requests=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                page.on('request',lambda req:requests.append(req.url))
                page.goto(base+'/',wait_until='networkidle')
                expect(page.locator('h1').first).to_contain_text('커피는 챙기고')
                expect(page.locator('#map circle')).not_to_have_count(0)
                if run==1: page.screenshot(path=str(output/f'{mode}-01-home.png'),full_page=True)
                page.locator('#findRoutes').click()
                expect(page.locator('.route-card')).to_have_count(2)
                expect(page.locator('#routeArea')).to_contain_text('마감까지')
                if run==1: page.screenshot(path=str(output/f'{mode}-02-routes.png'),full_page=True)
                page.locator('[data-quote]').first.click()
                expect(page.locator('#orderArea')).to_be_visible()
                first=snapshot(page,base); oid=first['order']['id']; source=first['order']['store_id']
                page.locator('[data-action=busy]').click()
                expect(page.locator('#orderArea .rescue-banner')).to_be_visible()
                if run==1: page.screenshot(path=str(output/f'{mode}-03-rescue.png'),full_page=True)
                page.locator('[data-quote]').first.click()
                expect(page.locator('#confirmDialog')).to_be_visible()
                page.locator('#confirmTransfer').click()
                expect(page.locator('.success-banner')).to_be_visible()
                moved=snapshot(page,base)
                assert moved['order']['id']==oid and moved['order']['store_id']!=source
                assert moved['receipt']['authorization_count']==1
                assert moved['receipt']['capture_count']==0
                if run==1: page.screenshot(path=str(output/f'{mode}-04-transferred.png'),full_page=True)
                page.reload(wait_until='networkidle')
                expect(page.locator('#orderArea')).to_contain_text(oid)
                page.locator('nav [data-view=merchant]').click()
                expect(page.locator('#merchantContent')).to_contain_text(oid)
                advance=page.locator('#merchantContent [data-action=advance]')
                if advance.count(): advance.click()
                start=page.locator('#merchantContent [data-action=start]')
                expect(start).to_be_enabled(); start.click()
                expect(page.locator('#merchantContent .status-pill')).to_have_text('제조 중')
                page.locator('#merchantContent [data-action=advance]').click()
                ready=page.locator('#merchantContent [data-action=ready]')
                expect(ready).to_be_enabled();ready.click()
                expect(page.locator('#merchantContent .status-pill')).to_have_text('픽업 가능')
                if run==1:page.screenshot(path=str(output/f'{mode}-05-merchant.png'),full_page=True)
                page.locator('nav [data-view=customer]').click()
                code=page.locator('#pickupCode').inner_text().strip()
                page.locator('#claimCode').fill(code);page.locator('#claim').click()
                expect(page.locator('#orderArea .status-pill')).to_have_text('수령 완료')
                page.locator('nav [data-view=receipt]').click()
                expect(page.locator('#receiptContent')).to_contain_text(oid)
                final=snapshot(page,base)
                assert final['receipt']['capture_count']==1
                assert final['receipt']['net_paid']==final['order']['price']
                assert final['order']['id']==oid
                if run==1:page.screenshot(path=str(output/f'{mode}-06-receipt.png'),full_page=True)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
                assert errors==[],errors
                assert all(url.startswith(base+'/') or url.startswith('data:') for url in requests),requests
                results.append(dict(run=run,viewport=mode,result='passed',order_id=oid,
                    same_order=True,authorization_count=1,capture_count=1,page_errors=errors,
                    browser=browser.version,base_url=base,release_commit=health.get('release_commit'),
                    checks=['http','deadline_matching','rescue','consent','same_order_transfer','reload',
                            'merchant','pickup_code','one_capture','receipt','no_overflow','no_external_requests']))
                (output/'browser-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
                context.close()
        browser.close()
    return results

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--base-url',default='http://127.0.0.1:10000')
    parser.add_argument('--output',type=Path,default=Path('verification/route-product'))
    parser.add_argument('--repeat',type=int,default=3);parser.add_argument('--expected-commit')
    args=parser.parse_args();assert args.repeat>0
    print(json.dumps(verify(args.base_url.rstrip('/'),args.output,args.repeat,args.expected_commit),ensure_ascii=False))
