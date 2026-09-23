"""Real browser/API verification; does not use set_content or mocked fetch."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
from playwright.sync_api import sync_playwright


def run(output: Path, repeat: int):
    output.mkdir(parents=True,exist_ok=True)
    with socket.socket() as socket_:
        socket_.bind(('127.0.0.1',0)); port=socket_.getsockname()[1]
    base=f'http://127.0.0.1:{port}'
    log=(output/'server.log').open('w')
    process=subprocess.Popen([sys.executable,'-m','app.workbench','--port',str(port),
        '--db',str(output/'browser.sqlite')],stdout=log,stderr=subprocess.STDOUT)
    results=[]
    try:
        for _ in range(80):
            try:
                with urllib.request.urlopen(base+'/health',timeout=.5) as r:
                    if r.status==200: break
            except OSError:
                if process.poll() is not None: raise RuntimeError('server failed; inspect server.log')
                time.sleep(.15)
        else: raise RuntimeError('server readiness timed out')
        with sync_playwright() as pw:
            options={'headless':True,'args':['--no-sandbox','--disable-dev-shm-usage']}
            if os.getenv('REPAIR_BROWSER_PATH'): options['executable_path']=os.environ['REPAIR_BROWSER_PATH']
            browser=pw.chromium.launch(**options)
            from playwright.sync_api import expect
            for pass_no in range(1,repeat+1):
                for mode,viewport in [('desktop',{'width':1440,'height':1100}),('mobile',{'width':390,'height':844})]:
                    context=browser.new_context(viewport=viewport,locale='ko-KR')
                    page=context.new_page(); errors=[]; requests=[]
                    page.on('pageerror',lambda e:errors.append(str(e)))
                    page.on('request',lambda r:requests.append(r.url))
                    page.goto(base+'/repair-lab'); expect(page.locator('#decision')).to_have_text('계획 생성 가능')
                    page.get_by_role('button',name='계획 조회',exact=True).click()
                    expect(page.locator('#planContent')).to_contain_text('9,000 KRW')
                    page.get_by_role('button',name='승인 후 모의 기록',exact=True).click()
                    expect(page.locator('#effectCount')).to_have_text('2개')
                    page.get_by_role('button',name='동일 승인 재전송',exact=True).click()
                    expect(page.locator('#message')).to_contain_text('중복 승인')
                    expect(page.locator('#effectCount')).to_have_text('2개')
                    page.reload(); expect(page.locator('#effectCount')).to_have_text('2개')
                    expect(page.locator('#message')).to_contain_text('복원')
                    page.locator('[data-case=missing_parent]').click()
                    expect(page.locator('#decision')).to_have_text('증거 대기')
                    expect(page.locator('#preview')).to_be_disabled()
                    page.get_by_role('button',name='누락 증거 받기',exact=True).click()
                    expect(page.locator('#decision')).to_have_text('계획 생성 가능')
                    page.locator('#preview').click(); expect(page.locator('#approve')).to_be_enabled()
                    page.locator('#approve').click(); expect(page.locator('#effectCount')).to_have_text('1개')
                    for case in ['conflicting_identity','terminal_conflict','partial_cancel','multiple_postings']:
                        page.locator(f'[data-case={case}]').click()
                        expect(page.locator('#decision')).to_have_text('자동 복구 차단')
                        expect(page.locator('#preview')).to_be_disabled()
                        expect(page.locator('#approve')).to_be_disabled()
                    page.locator('[data-case=delayed_cancel]').click()
                    expect(page.locator('#decision')).to_have_text('계획 생성 가능')
                    page.locator('#preview').click(); expect(page.locator('#approve')).to_be_enabled()
                    sid=page.evaluate("localStorage.getItem('pickup-pact.repair-session')")
                    # Another actor changes server evidence while this screen is stale.
                    r=context.request.post(base+f'/api/repair-lab/sessions/{sid}/simulate',data={'action':'conflict'})
                    assert r.status==200
                    page.locator('#approve').click()
                    expect(page.locator('#message')).to_contain_text('요청이 차단')
                    page.locator('#refresh').click(); expect(page.locator('#effectCount')).to_have_text('0개')
                    expect(page.locator('#decision')).to_have_text('자동 복구 차단')
                    page.locator('[data-case=delayed_cancel]').click()
                    expect(page.locator('#decision')).to_have_text('계획 생성 가능')
                    page.locator('#preview').click(); expect(page.locator('#approve')).to_be_enabled()
                    page.evaluate('window.scrollTo(0,0)')
                    overflow=page.evaluate('document.documentElement.scrollWidth > window.innerWidth')
                    assert not overflow, f'{mode}: horizontal overflow'
                    assert not errors, errors
                    assert all(url.startswith(base) for url in requests),requests
                    page.screenshot(path=str(output/f'{mode}-pass{pass_no}.png'),full_page=True)
                    results.append({'pass':pass_no,'viewport':mode,'browser':browser.version,
                        'checks':['real_http','approval','duplicate','reload','missing_then_resolved',
                                  'four_blocked_cases','stale_approval_rejected','no_external_requests','no_horizontal_overflow'],
                        'page_errors':errors,'http_requests':len(requests),'result':'passed'})
                    context.close()
            browser.close()
    finally:
        process.terminate()
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired: process.kill();process.wait()
        log.close()
    (output/'browser-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'verified_flows':len(results),'repeat':repeat,'result':'passed'},ensure_ascii=False))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=Path('verification/workbench'))
    parser.add_argument('--repeat',type=int,default=3);a=parser.parse_args();run(a.output,a.repeat)
