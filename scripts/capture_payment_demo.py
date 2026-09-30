#!/usr/bin/env python3
"""Record the integrated synthetic payment flow against actual HTTP servers."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import subprocess
import time
from urllib.request import urlopen
from playwright.sync_api import expect, sync_playwright


def capture(base: str, expected: str, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    raw = output / 'raw'; raw.mkdir(exist_ok=True)
    def get(path):
        with urlopen(base + path, timeout=30) as response:
            return json.load(response)
    before = get('/health')
    assert before['release_commit'] == expected, before
    runtime = get('/api/route/runtime')
    assert runtime['automatic_recovery'] and runtime['merchant_transport'] == 'http' and runtime['payment_transport']=='http' and runtime['payment_storage_ready'], runtime
    scenes, errors, requests = [], [], []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width':1280, 'height':900}, locale='ko-KR',
            record_video_dir=str(raw), record_video_size={'width':1280, 'height':900})
        try:
            t0 = time.monotonic(); page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('request', lambda req: requests.append({'url':req.url, 'method':req.method,
                'body':req.post_data_json if req.method=='POST' and '/api/route/' in req.url else None}))
            def idle():
                page.wait_for_function("!document.body.classList.contains('busy')")
            def stage(value):
                expect(page.locator('#guidePanel')).to_have_attribute('data-stage', value, timeout=20000)
            def state():
                sid = page.evaluate("localStorage.getItem('pickup-pact.route-journey')")
                return get('/api/route/journeys/' + sid)
            def shot(name):
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                page.screenshot(path=str(output/(name+'.png')), full_page=False)
            @contextmanager
            def scene(name, caption, seconds):
                start = time.monotonic()-t0
                yield
                page.wait_for_timeout(max(1.5, seconds-(time.monotonic()-t0-start))*1000)
                scenes.append({'name':name,'caption':caption,'start':start,'duration':time.monotonic()-t0-start})
                shot(name)
            page.goto(base+'/?payment=authorize_reply_lost',wait_until='networkidle')
            expect(page.locator('#quickStart')).to_be_enabled()
            with scene('01-home','설정 없이 시작 · 실제 서버를 사용하는 주문 체험',4):
                expect(page.locator('#coupon')).not_to_be_visible()
            with scene('02-order','승인이 저장된 뒤 응답이 끊겨도 · 같은 승인 번호로 재확인',4):
                page.locator('#quickStart').click(); stage('busy'); idle()
                page.locator('#guidePanel').scroll_into_view_if_needed()
            original=state();oid=original['order']['id']
            assert original['order']['price']==2800 and original['wallet']['held_points']==1000
            page.locator('#guideAction').click();stage('reject');idle()
            page.locator('#guideAction').click();expect(page.locator('#confirmDialog')).to_be_visible()
            with scene('03-consent','금액과 변경 조건을 직접 확인 · 동의 전에는 옮기지 않아요',5):
                expect(page.locator('#transferAgreement')).to_contain_text('360mL')
            with scene('04-rejected','새 매장이 거절해도 원래 주문·쿠폰·포인트는 그대로',6):
                page.locator('#confirmTransfer').click();stage('disconnect');idle()
                page.locator('#guidePanel').scroll_into_view_if_needed()
            rejected=state()
            assert original['order']==rejected['order'] and original['wallet']==rejected['wallet']
            page.locator('#guideAction').click();expect(page.locator('#confirmDialog')).to_be_visible()
            with scene('05-automatic-recovery','응답만 끊긴 상황 · 다시 누르지 않아도 서버가 자동 확인',7):
                page.locator('#confirmTransfer').click();stage('waiting')
                pending=state();assert pending['handoff_pending']
                page.locator('#guidePanel').scroll_into_view_if_needed();shot('05-pending')
                stage('prepare')
            recovered=state()
            assert recovered['order']['id']==oid and recovered['order']['store_id']=='oat'
            with scene('06-recovered','같은 주문 번호로 새 매장에 연결 · 중복 결제 없이 이어가요',4):
                page.locator('#guidePanel').scroll_into_view_if_needed()
            with scene('07-merchant','체험 시간을 앞당겨 제조 · 실제 대기 시간 측정은 아니에요',5):
                page.locator('#guideAction').click();stage('ready');idle()
                page.locator('#guideAction').click();stage('claim');idle()
            with scene('08-receipt','주문·매장·결제·혜택 원본 대조 · 3,200원 청구 1건',6):
                page.locator('#guideAction').click();stage('receipt');idle()
                page.locator('#guideAction').click();expect(page.locator('#receiptView')).to_be_visible()
                expect(page.locator('#finalProof')).to_have_attribute('data-status','MATCH',timeout=15000)
                page.locator('#finalProof').scroll_into_view_if_needed()
            final=state()
            assert final['order']['id']==oid and final['receipt']['capture_count']==1
            assert final['receipt']['net_paid']==3200 and final['wallet']['spent']==1000 and final['wallet']['earned']==32
            assert not any(r['body'] and r['body'].get('action')=='recover' for r in requests)
            assert not errors, errors
            video=page.video;context.close();assert video
            after=get('/health');assert after['release_commit']==expected
            report={'base_url':base,'recorded_release_commit':expected,'health_before':before,'health_after':after,
                'recording_scope':'public' if base.startswith('https://pickup-pact-demo.onrender.com') else 'ci_local_http',
                'runtime':runtime,'browser':browser.version,'source_video':str(Path(video.path()).resolve().relative_to(output.resolve())),
                'scenes':scenes,'same_order':True,'refusal_preserved_order_and_benefits':True,
                'recovery_without_manual_command':True,'order_id':oid,'cash_due':3200,'capture_count':1,
                'points_spent':1000,'points_earned':32,'page_errors':errors,'requests':requests,
                'editing':'Original Chromium pixels; normal-speed scene cuts and caption padding only. Virtual clock advances are explicit.'}
            (output/'capture.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
            return report
        except Exception as error:
            (output/'failure.json').write_text(json.dumps({'error':str(error),'page_errors':errors,'requests':requests},ensure_ascii=False,indent=2))
            raise
        finally:
            context.close();browser.close()


def edit(output: Path, report: dict):
    font=Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc')
    assert font.exists(),'Korean caption font missing'
    source=output/report['source_video'];clips=[]
    def run(*args):subprocess.run(list(args),check=True)
    scope='공개 데모' if report['recording_scope']=='public' else 'CI 실제 HTTP 서버'
    for i,s in enumerate(report['scenes']):
        caption=output/f'caption-{i}.txt';caption.write_text(s['caption'])
        clip=output/f'clip-{i}.mp4'
        vf=(f'pad=1280:978:0:0:color=0x153e35,drawtext=fontfile={font}:textfile={caption}:'
            f'fontsize=22:fontcolor=white:x=(w-tw)/2:y=920,drawtext=fontfile={font}:'
            f"text='{scope} 녹화 · 가상 매장 / 실제 결제 없음':fontsize=14:fontcolor=0xd7ec99:x=(w-tw)/2:y=954")
        run('ffmpeg','-y','-v','error','-ss',str(s['start']),'-i',str(source),'-t',str(s['duration']),
            '-vf',vf,'-an','-r','25','-c:v','libx264','-preset','veryfast','-crf','22','-pix_fmt','yuv420p',str(clip))
        clips.append(clip)
    listing=output/'concat.txt';listing.write_text(''.join(f"file '{p.resolve()}'\n" for p in clips))
    final=output/'pickup-pact-payment.mp4'
    run('ffmpeg','-y','-v','error','-f','concat','-safe','0','-i',str(listing),'-c','copy','-movflags','+faststart',str(final))
    run('ffmpeg','-y','-v','error','-i',str(final),'-filter_complex',
        'fps=5,scale=800:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4',
        '-loop','0',str(output/'pickup-pact-payment.gif'))
    for target in [final,output/'pickup-pact-payment.gif']:
        run('ffmpeg','-v','error','-xerror','-i',str(target),'-f','null','-')
    duration=float(subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration','-of','default=noprint_wrappers=1:nokey=1',str(final)],text=True))
    assert 35<duration<70,duration
    report['edited_duration_seconds']=duration
    report['media_sha256']={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in output.iterdir()
        if p.suffix in {'.png','.gif','.mp4'} and not p.name.startswith('clip-')}
    (output/'capture.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    for path in clips:path.unlink()
    for path in output.glob('caption-*.txt'):path.unlink()
    listing.unlink()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url',default='http://127.0.0.1:10000')
    parser.add_argument('--expected-commit',required=True)
    parser.add_argument('--output',type=Path,default=Path('verification/payment-media'))
    args=parser.parse_args();report=capture(args.base_url.rstrip('/'),args.expected_commit,args.output)
    edit(args.output,report)
    print(json.dumps({k:report[k] for k in ['recorded_release_commit','recording_scope','edited_duration_seconds','capture_count']},ensure_ascii=False))
