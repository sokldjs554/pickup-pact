#!/usr/bin/env python3
"""Real public browser recording. No fake responses or application changes."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.request import urlopen
from playwright.sync_api import sync_playwright, expect


def health(base, expected):
    with urlopen(base+'/health',timeout=40) as r: value=json.load(r)
    assert value.get('release_commit')==expected,value
    return value


def capture(base,expected,output):
    output.mkdir(parents=True,exist_ok=True)
    raw=output/'raw';raw.mkdir(exist_ok=True)
    before=health(base,expected);scenes=[];screenshots=[];errors=[];responses=[]
    with sync_playwright() as p:
        kwargs={'headless':True}
        if os.environ.get('ROUTE_BROWSER_PATH'):kwargs['executable_path']=os.environ['ROUTE_BROWSER_PATH']
        browser=p.chromium.launch(**kwargs)
        context=browser.new_context(viewport={'width':1280,'height':900},
            record_video_dir=str(raw),record_video_size={'width':1280,'height':900})
        t0=time.monotonic();page=context.new_page()
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.on('response',lambda r:responses.append({'status':r.status,'url':r.url}) if r.url.startswith(base+'/api/') else None)
        page.goto(base+'/',wait_until='networkidle',timeout=60000)
        expect(page.locator('#map circle')).not_to_have_count(0)
        def snapshot(name):
            filename=name+'.png';page.screenshot(path=str(output/filename),full_page=False);screenshots.append(filename)
        @contextmanager
        def scene(name,caption,seconds):
            start=time.monotonic()-t0
            yield
            remaining=seconds-(time.monotonic()-t0-start)
            page.wait_for_timeout(max(remaining,2.0)*1000)
            scenes.append({'name':name,'caption':caption,'start':start,'duration':time.monotonic()-t0-start})
            snapshot(name)
        def data():
            sid=page.evaluate("localStorage.getItem('pickup-pact.route-journey')")
            r=page.request.get(base+'/api/route/journeys/'+sid);assert r.ok
            return r.json()
        with scene('01-home','일정에 맞춘 커피 주문 · 실제 공개 데모',3):
            expect(page.locator('h1').first).to_contain_text('커피는 챙기고')
        with scene('02-benefits','매장 전용 쿠폰과 1,000P를 선택합니다',5):
            page.locator('#coupon').scroll_into_view_if_needed()
            page.locator('#coupon').select_option('wave1000');page.locator('#points').fill('1000')
        with scene('03-order','쿠폰·포인트 적용 후 2,300원으로 주문',4):
            page.locator('#findRoutes').click()
            wave=page.locator('.route-card[data-hover=wave]')
            expect(wave).to_contain_text('2,300원');wave.scroll_into_view_if_needed()
        wave.locator('[data-quote]').click()
        expect(page.locator('#availablePoints')).to_have_text('1,000P')
        original=data();oid=original['order']['id']
        with scene('04-delay','혼잡을 추가하면, 유지·이동의 시간과 비용이 달라집니다',5):
            page.locator('[data-action=busy]').click()
            expect(page.locator('#orderComparison')).to_contain_text('11분')
            page.locator('#orderComparison').scroll_into_view_if_needed()
        with scene('05-consent','11분 빠른 도착 예상 / 쿠폰 상실로 1,400원 추가',7):
            page.locator('.route-card[data-hover=oat] [data-quote]').click()
            expect(page.locator('#couponLossWarning')).to_contain_text('1,000원')
            expect(page.locator('#dialogBody .benefit-total')).to_contain_text('3,700원')
        with scene('06-transfer','동의한 금액으로 이동합니다. 주문 번호는 그대로입니다',4):
            page.locator('#confirmTransfer').click()
            expect(page.locator('.success-banner')).to_be_visible()
            page.locator('#orderArea').scroll_into_view_if_needed()
        moved=data()
        assert moved['order']['id']==oid and moved['order']['store_id']=='oat'
        assert moved['receipt']['capture_count']==0
        # Existing virtual clock: not real manufacturing time.
        page.locator('nav [data-view=merchant]').click()
        advance=page.locator('#merchantContent [data-action=advance]')
        if advance.count():advance.click()
        start=page.locator('#merchantContent [data-action=start]');expect(start).to_be_enabled();start.click()
        expect(page.locator('#merchantContent .status-pill')).to_have_text('제조 중')
        page.locator('#merchantContent [data-action=advance]').click()
        ready=page.locator('#merchantContent [data-action=ready]');expect(ready).to_be_enabled();ready.click()
        snapshot('07-merchant')
        page.locator('nav [data-view=customer]').click()
        with scene('08-pickup','가상 시계를 진행한 뒤 수령 · 모의 결제는 한 번만',5):
            code=page.locator('#pickupCode').inner_text().strip()
            page.locator('#claimCode').fill(code);page.locator('#claim').click()
            expect(page.locator('#benefitsEarned')).to_contain_text('37P')
            page.locator('#benefitsEarned').scroll_into_view_if_needed()
        with scene('09-receipt','같은 주문의 영수증 · 3,700원 / 1,000P 사용·37P 적립',5):
            page.locator('nav [data-view=receipt]').click()
            expect(page.locator('#receiptContent')).to_contain_text(oid)
            page.locator('#receiptContent').scroll_into_view_if_needed()
        final=data()
        assert final['order']['id']==oid and final['receipt']['capture_count']==1
        assert final['receipt']['net_paid']==3700 and final['wallet']['available_points']==1037
        video=page.video;context.close();assert video is not None
        source=Path(video.path())
        mobile=browser.new_context(viewport={'width':390,'height':844})
        mp=mobile.new_page();mp.on('pageerror',lambda e:errors.append(str(e)))
        mp.goto(base+'/',wait_until='networkidle',timeout=60000)
        mp.screenshot(path=str(output/'mobile-home.png'),full_page=True)
        mp.locator('#coupon').select_option('wave1000');mp.locator('#points').fill('1000')
        mp.locator('#findRoutes').click()
        card=mp.locator('.route-card[data-hover=wave]');expect(card).to_contain_text('2,300원')
        card.locator('[data-quote]').click();mp.locator('[data-action=busy]').click()
        expect(mp.locator('#orderComparison')).to_contain_text('11분')
        mp.locator('.route-card[data-hover=oat] [data-quote]').click()
        expect(mp.locator('#couponLossWarning')).to_contain_text('1,000원')
        mp.screenshot(path=str(output/'mobile-consent.png'),full_page=False)
        mp.locator('#confirmTransfer').click();mp.locator('[data-action=cancel]').click()
        expect(mp.locator('#availablePoints')).to_have_text('2,000P')
        mp.locator('#benefitsRestored').scroll_into_view_if_needed()
        mp.screenshot(path=str(output/'mobile-restored.png'),full_page=False)
        assert mp.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        mobile.close();browser_version=browser.version;browser.close()
    after=health(base,expected)
    assert not errors,errors
    assert all(r['status']<400 for r in responses),responses
    report={'base_url':base,'recorded_release_commit':expected,'health_before':before,'health_after':after,
        'browser':browser_version,'viewport':{'width':1280,'height':900},'source_video':str(source.resolve().relative_to(output.resolve())),
        'scenes':scenes,'screenshots':screenshots,'order_id':oid,'same_order':True,'cash_due':3700,
        'capture_count':1,'points_spent':1000,'points_earned':37,'wallet_available':1037,
        'page_errors':errors,'api_responses':responses,
        'editing':'Real browser recording; between-scene waiting/preparation omitted. Normal-speed scenes. Virtual clock and simulated payments.'}
    (output/'capture.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    return report


def edit(output,report):
    font=Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc')
    if not font.exists():raise RuntimeError('Korean caption font missing')
    source=output/report['source_video'];clips=[]
    def run(*args):subprocess.run(list(args),check=True)
    for i,s in enumerate(report['scenes']):
        text=output/f'caption-{i}.txt';text.write_text(s['caption']);clip=output/f'clip-{i}.mp4'
        vf=("pad=1280:978:0:0:color=0x153e35,"
            f"drawtext=fontfile={font}:textfile={text}:fontsize=23:fontcolor=white:x=(w-tw)/2:y=920,"
            f"drawtext=fontfile={font}:text='공개 데모 녹화 · 가상 매장 / 모의 결제':fontsize=14:fontcolor=0xd7ec99:x=(w-tw)/2:y=954")
        run('ffmpeg','-y','-loglevel','error','-ss',str(s['start']),'-i',str(source),'-t',str(s['duration']),
            '-vf',vf,'-an','-r','25','-c:v','libx264','-preset','veryfast','-crf','22','-pix_fmt','yuv420p',str(clip))
        clips.append(clip)
    listing=output/'concat.txt';listing.write_text(''.join(f"file '{p.resolve()}'\n" for p in clips))
    final=output/'pickup-pact-demo.mp4'
    run('ffmpeg','-y','-loglevel','error','-f','concat','-safe','0','-i',str(listing),'-c','copy','-movflags','+faststart',str(final))
    run('ffmpeg','-y','-loglevel','error','-i',str(final),'-filter_complex',
        'fps=5,scale=800:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4',
        '-loop','0',str(output/'pickup-pact-preview.gif'))
    duration=float(subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration','-of',
        'default=noprint_wrappers=1:nokey=1',str(final)],text=True).strip())
    assert 30<duration<65,duration
    report['edited_duration_seconds']=round(duration,2)
    report['media_sha256']={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in output.iterdir()
        if p.suffix in {'.png','.gif','.mp4'} and not p.name.startswith('clip-')}
    (output/'capture.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    for p in clips:p.unlink()
    for p in output.glob('caption-*.txt'):p.unlink()
    listing.unlink()

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--base-url',default='https://pickup-pact-demo.onrender.com')
    p.add_argument('--expected-commit',required=True)
    p.add_argument('--output',type=Path,default=Path('verification/demo-media'))
    a=p.parse_args();report=capture(a.base_url.rstrip('/'),a.expected_commit,a.output);edit(a.output,report)
    print(json.dumps({k:report[k] for k in ['recorded_release_commit','edited_duration_seconds','same_order','capture_count','page_errors']},ensure_ascii=False))
