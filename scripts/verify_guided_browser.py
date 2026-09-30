#!/usr/bin/env python3
"""Exercise the guided entry against real HTTP, including page-closed recovery."""
from __future__ import annotations
import argparse
import json
import os
import time
from pathlib import Path
from urllib.request import urlopen
from playwright.sync_api import sync_playwright, expect


def verify(base: str, output: Path, repeat: int = 3, record: bool = False, expected_commit: str | None = None):
    output.mkdir(parents=True, exist_ok=True)
    with urlopen(base + '/health', timeout=20) as r:
        health = json.load(r)
    if expected_commit:
        assert health['release_commit'] == expected_commit, health
    with urlopen(base + '/api/route/runtime', timeout=20) as r:
        runtime = json.load(r)
    assert runtime['automatic_recovery'] and runtime['merchant_transport'] == 'http', runtime
    results = []
    with sync_playwright() as p:
        options = {'headless': True}
        if os.environ.get('ROUTE_BROWSER_PATH'):
            options['executable_path'] = os.environ['ROUTE_BROWSER_PATH']
        browser = p.chromium.launch(**options)
        try:
            for run in range(1, repeat + 1):
                for mode, width, height in [('desktop', 1440, 1000), ('mobile', 390, 844)]:
                    context_options = {'viewport': {'width': width, 'height': height}}
                    if record and run == 1 and mode == 'desktop':
                        context_options.update(record_video_dir=str(output/'recording'), record_video_size={'width': width, 'height': height})
                    context = browser.new_context(**context_options)
                    page = context.new_page()
                    errors, commands, responses = [], [], []
                    page.on('pageerror', lambda e: errors.append(str(e)))
                    def capture(response):
                        request = response.request
                        if request.method == 'POST' and '/api/route/' in request.url:
                            commands.append({'url': request.url, 'body': request.post_data_json})
                            try:
                                responses.append({'status': response.status, 'body': response.json()})
                            except Exception:
                                responses.append({'status': response.status, 'body': None})
                    page.on('response', capture)
                    def idle():
                        page.wait_for_function("!document.body.classList.contains('busy')")
                    def stage(value):
                        expect(page.locator('#guidePanel')).to_have_attribute('data-stage', value, timeout=20000)
                    def snapshot():
                        sid = page.evaluate("localStorage.getItem('pickup-pact.route-journey')")
                        r = page.request.get(base + '/api/route/journeys/' + sid)
                        assert r.ok, r.text()
                        return r.json()
                    def shot(name):
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), name
                        if run == 1:
                            page.screenshot(path=str(output/f'{mode}-{name}.png'), full_page=False)
                    try:
                        page.goto(base+'/', wait_until='networkidle')
                        assert page.locator('#quickStart').count() == 1, 'Default entry lacks the quick-start action.'
                        expect(page.locator('#quickStart')).to_be_enabled()
                        assert page.locator('#customizeOrder').get_attribute('open') is None
                        expect(page.locator('#coupon')).not_to_be_visible()
                        shot('01-home')
                        page.locator('#quickStart').dblclick()
                        stage('busy'); idle()
                        before = snapshot(); oid = before['order']['id']
                        assert before['order']['price'] == 2800 and before['wallet']['held_points'] == 1000
                        assert sum(c['url'].endswith('/journeys') for c in commands) == 1, commands
                        page.locator('#guideAction').click(); stage('reject'); idle()
                        page.locator('#guideAction').click()
                        expect(page.locator('#confirmDialog')).to_be_visible()
                        expect(page.locator('#transferAgreement')).to_contain_text('360mL')
                        page.locator('#confirmTransfer').click(); stage('disconnect'); idle()
                        rejected = snapshot()
                        assert rejected['order'] == before['order'] and rejected['wallet'] == before['wallet']
                        shot('02-rejected')
                        page.locator('#guideAction').click()
                        expect(page.locator('#confirmDialog')).to_be_visible()
                        page.locator('#confirmTransfer').click(); stage('waiting')
                        pending = snapshot(); assert pending['handoff_pending']
                        expect(page.locator('#guideAction')).to_be_disabled(); shot('03-pending')
                        # Closing this page stops GET polling. Only the server worker may recover.
                        page.close()
                        deadline = time.monotonic()+20
                        while True:
                            r = context.request.get(base+'/api/route/journeys/'+pending['id'])
                            assert r.ok
                            recovered = r.json()
                            if not recovered['handoff_pending']:
                                break
                            assert time.monotonic() < deadline, recovered
                            time.sleep(.5)
                        assert recovered['order']['id'] == oid and recovered['order']['store_id'] == 'oat'
                        page = context.new_page(); page.on('pageerror', lambda e: errors.append(str(e))); page.on('response', capture)
                        page.goto(base+'/', wait_until='networkidle'); stage('prepare')
                        expect(page.locator('#quickStart')).not_to_be_visible(); shot('04-recovered')
                        page.locator('#guideAction').click(); stage('ready'); idle()
                        page.locator('#guideAction').click(); stage('claim'); idle()
                        page.locator('#guideAction').click(); stage('receipt'); idle()
                        final = snapshot()
                        assert final['order']['id'] == oid
                        assert final['receipt']['capture_count'] == 1 and final['receipt']['net_paid'] == 3200
                        assert final['wallet']['spent'] == 1000 and final['wallet']['earned'] == 32
                        page.locator('#guideAction').click()
                        expect(page.locator('#receiptView')).to_be_visible(); shot('05-receipt')
                        assert not any(c['body'].get('action') == 'recover' for c in commands), commands
                        assert not errors, errors
                        results.append({'repetition': run, 'viewport': mode, 'result': 'passed', 'order_id': oid,
                                        'capture_count': final['receipt']['capture_count'], 'paid': final['receipt']['net_paid'],
                                        'recovery_without_page': True, 'manual_recover_requests': 0,
                                        'release_commit': health['release_commit'], 'base_url': base})
                        (output/f'{mode}-{run}-network.json').write_text(json.dumps({'commands': commands, 'responses': responses}, ensure_ascii=False, indent=2))
                        (output/'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2))
                    except Exception as e:
                        if not page.is_closed():
                            page.screenshot(path=str(output/f'failure-{mode}-{run}.png'), full_page=True)
                        (output/'failure.json').write_text(json.dumps({'error': str(e), 'completed': results, 'page_errors': errors}, ensure_ascii=False, indent=2))
                        raise
                    finally:
                        context.close()
        finally:
            browser.close()
    with urlopen(base + '/health', timeout=20) as response:
        after = json.load(response)
    assert after['release_commit'] == health['release_commit'], (health, after)
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:10000')
    parser.add_argument('--output', type=Path, default=Path('verification/guided-browser'))
    parser.add_argument('--repeat', type=int, default=3)
    parser.add_argument('--record', action='store_true')
    parser.add_argument('--expected-commit')
    args = parser.parse_args()
    if not 1 <= args.repeat <= 10:
        parser.error('repeat must be between 1 and 10')
    print(json.dumps(verify(args.base_url.rstrip('/'), args.output, args.repeat, args.record, args.expected_commit), ensure_ascii=False, indent=2))
