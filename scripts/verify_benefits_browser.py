#!/usr/bin/env python3
"""Real HTTP benefit/transfer/cancellation/pickup and evidence interactions."""
from argparse import ArgumentParser
import json
import os
from pathlib import Path
from urllib.request import urlopen
from playwright.sync_api import sync_playwright, expect


def verify(base, output, repeat=3, expected_commit=None):
    output.mkdir(parents=True,exist_ok=True)
    with urlopen(base+'/health',timeout=30) as r: health=json.load(r)
    if expected_commit: assert health['release_commit']==expected_commit,health
    reports=[]
    with sync_playwright() as p:
        kwargs={'headless':True}
        if os.environ.get('ROUTE_BROWSER_PATH'): kwargs['executable_path']=os.environ['ROUTE_BROWSER_PATH']
        browser=p.chromium.launch(**kwargs)
        for n in range(1,repeat+1):
            for mode,width,height in [('desktop',1440,1100),('mobile',390,844)]:
                context=browser.new_context(viewport={'width':width,'height':height})
                page=context.new_page();errors=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(base+'/',wait_until='networkidle')
                page.locator('#coupon').select_option('wave1000')
                page.locator('#points').fill('1000')
                page.locator('#findRoutes').click()
                wave=page.locator('.route-card[data-hover=wave]')
                expect(wave).to_contain_text('2,300원')
                wave.locator('[data-quote]').click()
                expect(page.locator('#availablePoints')).to_have_text('1,000P')
                sid=page.evaluate("localStorage.getItem('pickup-pact.route-journey')")
                data=page.request.get(base+'/api/route/journeys/'+sid).json();oid=data['order']['id']
                page.locator('[data-action=busy]').click()
                expect(page.locator('#orderComparison')).to_contain_text('11분')
                page.locator('.route-card[data-hover=oat] [data-quote]').click()
                expect(page.locator('#couponLossWarning')).to_contain_text('1,000원')
                expect(page.locator('#dialogBody .benefit-total')).to_contain_text('3,700원')
                if n==1:page.screenshot(path=str(output/f'{mode}-coupon-loss-consent.png'),full_page=True)
                page.locator('#confirmTransfer').click()
                expect(page.locator('#orderArea')).to_contain_text(oid)
                page.locator('[data-action=cancel]').click()
                expect(page.locator('#benefitsRestored')).to_be_visible()
                expect(page.locator('#availablePoints')).to_have_text('2,000P')
                page.reload(wait_until='networkidle')
                expect(page.locator('#availablePoints')).to_have_text('2,000P')
                assert page.request.get(base+'/api/route/journeys/'+sid).json()['wallet']['held_coupon'] is None
                if n==1:page.screenshot(path=str(output/f'{mode}-restored-wallet.png'),full_page=True)
                page.locator('#reset').click();page.wait_for_load_state('networkidle')
                page.locator('#coupon').select_option('welcome500');page.locator('#points').fill('1000')
                page.locator('#findRoutes').click()
                page.locator('.route-card[data-hover=wave] [data-quote]').click()
                expect(page.locator('#orderArea .benefit-total')).to_contain_text('2,800원')
                page.locator('nav [data-view=merchant]').click()
                advance=page.locator('#merchantContent [data-action=advance]')
                if advance.count():advance.click()
                start=page.locator('#merchantContent [data-action=start]');expect(start).to_be_enabled();start.click()
                page.locator('#merchantContent [data-action=advance]').click()
                ready=page.locator('#merchantContent [data-action=ready]');expect(ready).to_be_enabled();ready.click()
                page.locator('nav [data-view=customer]').click()
                code=page.locator('#pickupCode').inner_text().strip();page.locator('#claimCode').fill(code)
                page.locator('#claim').click()
                expect(page.locator('#benefitsEarned')).to_contain_text('28P')
                expect(page.locator('#availablePoints')).to_have_text('1,028P')
                page.locator('nav [data-view=receipt]').click()
                expect(page.locator('#receiptContent .benefit-total')).to_contain_text('2,800원')
                expect(page.locator('#receiptContent')).to_contain_text('1000P / 28P')
                if n==1:page.screenshot(path=str(output/f'{mode}-benefits-receipt.png'),full_page=True)
                page.locator('#runExperiment').click()
                expect(page.locator('#downloadExperiment')).to_be_visible(timeout=30000)
                expect(page.locator('#experimentResults')).to_contain_text('120건')
                expect(page.locator('.experiment-table tbody tr')).to_have_count(6)
                if n==1:page.screenshot(path=str(output/f'{mode}-paired-evidence.png'),full_page=True)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                assert not errors,errors
                reports.append(dict(pass_number=n,viewport=mode,status='passed',release_commit=health.get('release_commit'),
                    same_order_transfer=True,coupon_loss_disclosed=True,cancellation_restores=True,
                    pickup_cash_due=2800,points_spent=1000,points_earned=28,wallet_after_pickup=1028,
                    experiment_cases=120,page_errors=errors))
                (output/'browser-results.json').write_text(json.dumps(reports,ensure_ascii=False,indent=2))
                context.close()
        browser.close()
    return reports

if __name__=='__main__':
    p=ArgumentParser();p.add_argument('--base-url',default='http://127.0.0.1:10000')
    p.add_argument('--output',type=Path,default=Path('verification/benefits-browser'))
    p.add_argument('--repeat',type=int,default=3);p.add_argument('--expected-commit')
    args=p.parse_args();assert 1<=args.repeat<=10
    print(json.dumps(verify(args.base_url.rstrip('/'),args.output,args.repeat,args.expected_commit),ensure_ascii=False))
