#!/usr/bin/env python3
"""Actual customer/merchant tabs racing unchanged HTTP start and cancel requests."""
from __future__ import annotations
import argparse
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright, expect


async def verify(base: str, output: Path, repeat: int, expected_commit: str):
    output.mkdir(parents=True,exist_ok=True)
    reports=[]
    async with async_playwright() as pw:
        browser=await pw.chromium.launch(headless=True)
        try:
            for repetition in range(1,repeat+1):
                for mode,width,height in [('desktop',1440,1000),('mobile',390,844)]:
                    context=await browser.new_context(viewport={'width':width,'height':height},locale='ko-KR')
                    context.set_default_timeout(20000)
                    await context.tracing.start(screenshots=True,snapshots=True,sources=True)
                    errors=[];requests=[];responses=[]
                    try:
                        health=await (await context.request.get(base+'/health')).json()
                        assert health['release_commit']==expected_commit,health
                        customer=await context.new_page()
                        customer.on('pageerror',lambda e:errors.append(str(e)))
                        await customer.goto(base+'/',wait_until='networkidle')
                        await expect(customer.locator('#quickStart')).to_be_enabled()
                        await customer.locator('#quickStart').click()
                        await expect(customer.locator('#guidePanel')).to_have_attribute('data-stage','busy')
                        await customer.wait_for_function("!document.body.classList.contains('busy')")
                        sid=await customer.evaluate("localStorage.getItem('pickup-pact.route-journey')")
                        await customer.locator('nav [data-view=merchant]').click()
                        advance=customer.locator('#merchantContent [data-action=advance]')
                        if await advance.count():
                            await advance.click()
                            await customer.wait_for_function("!document.body.classList.contains('busy')")
                        await expect(customer.locator('#merchantContent [data-action=start]')).to_be_enabled()
                        snapshot=await (await context.request.get(base+'/api/route/journeys/'+sid)).json()
                        oid=snapshot['order']['id'];version=snapshot['version']
                        merchant=await context.new_page();observer=await context.new_page()
                        for page in [merchant,observer]:
                            page.on('pageerror',lambda e:errors.append(str(e)))
                            await page.goto(base+'/',wait_until='networkidle')
                            await expect(page.locator('#guideOrderId')).to_have_text(oid)
                            await page.locator('nav [data-view=merchant]').click()
                        await customer.locator('nav [data-view=customer]').click()
                        await expect(customer.locator('#orderArea [data-action=cancel]')).to_be_enabled()
                        await expect(merchant.locator('#merchantContent [data-action=start]')).to_be_enabled()
                        gate=asyncio.Event()
                        async def simultaneous(route):
                            body=route.request.post_data_json
                            if body.get('action') not in {'start','cancel'}:
                                await route.continue_();return
                            requests.append(body)
                            if len(requests)==2:gate.set()
                            await asyncio.wait_for(gate.wait(),10)
                            # Delay only until both real UI requests arrive. No body,
                            # response, status or server state is substituted.
                            await route.continue_()
                        await context.route('**/api/route/journeys/*/commands',simultaneous)
                        for page in [customer,merchant]:
                            page.on('response',lambda r:responses.append(r.status) if r.request.method=='POST' and r.url.endswith('/commands') else None)
                        await asyncio.gather(customer.locator('#orderArea [data-action=cancel]').click(),
                                             merchant.locator('#merchantContent [data-action=start]').click())
                        for page in [customer,merchant]:
                            await page.wait_for_function("!document.body.classList.contains('busy')")
                        assert len(requests)==2 and {r['action'] for r in requests}=={'start','cancel'},requests
                        assert all(r['expected_version']==version for r in requests),requests
                        assert sorted(responses)==[200,409],responses
                        await context.unroute('**/api/route/journeys/*/commands',simultaneous)
                        final=await (await context.request.get(base+'/api/route/journeys/'+sid)).json()
                        assert final['order']['id']==oid and final['order']['state'] in {'PREPARING','CANCELLED'}
                        proof=await (await context.request.get(base+'/api/route/journeys/'+sid+'/reconciliation')).json()
                        assert proof['status']=='MATCH',proof
                        assert proof['payment']['capture_count']==0 and proof['payment']['captured_krw']==0
                        assert proof['payment']['held_krw']==(2800 if final['order']['state']=='PREPARING' else 0)
                        assert final['wallet']['spent']==0 and final['wallet']['held_points']==(1000 if final['order']['state']=='PREPARING' else 0)
                        for page in [customer,merchant,observer]:
                            await page.reload(wait_until='networkidle')
                            await expect(page.locator('#guideOrderId')).to_have_text(oid)
                            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                        await observer.locator('nav [data-view=merchant]').click()
                        await expect(observer.locator('#merchantTerminals [data-shop=wave]')).to_contain_text('제조 중' if final['order']['state']=='PREPARING' else '취소')
                        assert not errors,errors
                        after=await (await context.request.get(base+'/health')).json()
                        assert after['release_commit']==expected_commit
                        reports.append(dict(repetition=repetition,viewport=mode,order_id=oid,result='passed',
                            release_commit=expected_commit,requests=requests,http_statuses=responses,
                            final_order_state=final['order']['state'],proof=proof,page_errors=errors,
                            transport_note='Two original UI requests paused until both arrived; payloads/responses unchanged.'))
                        (output/'results.json').write_text(json.dumps(reports,ensure_ascii=False,indent=2))
                        if repetition==1:
                            await observer.locator('#merchantTerminals').screenshot(path=str(output/f'{mode}-terminal-after-race.png'))
                    except Exception as exc:
                        (output/'failure.json').write_text(json.dumps(dict(error=str(exc),completed=len(reports),requests=requests,responses=responses,page_errors=errors),ensure_ascii=False,indent=2))
                        raise
                    finally:
                        await context.tracing.stop(path=str(output/f'trace-{repetition}-{mode}.zip'))
                        await context.close()
        finally:await browser.close()
    return dict(count=len(reports),all_passed=True,expected_commit=expected_commit)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base-url',default='http://127.0.0.1:10000');p.add_argument('--expected-commit',required=True)
    p.add_argument('--repeat',type=int,default=3);p.add_argument('--output',type=Path,default=Path('verification/payment-tabs'))
    a=p.parse_args()
    if not 1<=a.repeat<=5:p.error('repeat must be from 1 to 5')
    print(json.dumps(asyncio.run(verify(a.base_url.rstrip('/'),a.output,a.repeat,a.expected_commit)),ensure_ascii=False))
