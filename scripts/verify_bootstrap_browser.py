#!/usr/bin/env python3
"""Real HTTP restore with a deliberately slow deferred-script response.

Only the test server delays delivery of the original benefits-ui.js bytes.
No browser routing, fake API response, or production recovery timing is used.
"""
from argparse import ArgumentParser
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen
from uuid import uuid4


def serve(port):
    import uvicorn
    from demo.main import app

    async def delayed_asset(scope, receive, send):
        if scope['type'] == 'http' and scope['path'] == '/route-assets/benefits-ui.js':
            await asyncio.sleep(1.5)
        await app(scope, receive, send)

    uvicorn.run(delayed_asset, host='127.0.0.1', port=port, log_level='warning')


def verify(output, repeat=3):
    from playwright.sync_api import sync_playwright, expect
    output.mkdir(parents=True, exist_ok=True)
    reports = []
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    with tempfile.TemporaryDirectory(prefix='pickup-bootstrap-') as tmp:
        env = os.environ.copy()
        env['ROUTE_DB'] = str(Path(tmp) / 'orders.sqlite')
        env['PYTHONPATH'] = os.pathsep.join([str(Path.cwd()), env.get('PYTHONPATH', '')])
        with (output / 'server.log').open('w') as log:
            process = subprocess.Popen([sys.executable, __file__, '--serve', str(port)],
                                       env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                for _ in range(100):
                    if process.poll() is not None:
                        raise RuntimeError('test HTTP server exited before health')
                    try:
                        with urlopen(base + '/health', timeout=1) as response:
                            assert json.load(response)['status'] == 'ok'
                        break
                    except OSError:
                        time.sleep(.1)
                else:
                    raise RuntimeError('test HTTP server did not start')

                def api(path, body=None):
                    request = Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                      headers={'Content-Type': 'application/json'})
                    with urlopen(request, timeout=15) as response:
                        return json.load(response)

                with sync_playwright() as engine:
                    options = {'headless': True}
                    if os.environ.get('ROUTE_BROWSER_PATH'):
                        options['executable_path'] = os.environ['ROUTE_BROWSER_PATH']
                    browser = engine.chromium.launch(**options)
                    try:
                        for n in range(1, repeat + 1):
                            for mode, width, height in [('desktop', 1440, 1100), ('mobile', 390, 844)]:
                                state = api('/api/route/journeys', {'coupon_id': 'welcome500', 'points': 1000})
                                quote = next(p for p in state['recommendations'] if p['store_id'] == 'wave')
                                for action, extra in [('reserve', {'quote_id': quote['quote_id']}), ('cancel', {})]:
                                    state = api(f"/api/route/journeys/{state['id']}/commands", {
                                        'action': action, 'expected_version': state['version'],
                                        'request_id': uuid4().hex, **extra})
                                    assert not state['handoff_pending'], state['handoff']
                                assert state['order']['state'] == 'CANCELLED'
                                assert state['wallet']['available_points'] == 2000
                                context = browser.new_context(viewport={'width': width, 'height': height})
                                context.add_init_script("localStorage.setItem('pickup-pact.route-journey'," + json.dumps(state['id']) + ");")
                                page = context.new_page()
                                errors = []
                                page.on('pageerror', lambda error: errors.append(str(error)))
                                timings = []
                                try:
                                    for navigation in ('initial', 'reload'):
                                        if navigation == 'initial':
                                            page.goto(base + '/', wait_until='networkidle')
                                        else:
                                            page.reload(wait_until='networkidle')
                                        expect(page.locator('#availablePoints')).to_have_text('2,000P')
                                        expect(page.locator('#benefitsRestored')).to_be_visible()
                                        expect(page.locator('#handoffPanel')).to_contain_text(state['order']['id'])
                                        entries = page.evaluate("""() => performance.getEntriesByType('resource')
                                            .map(e => ({path:new URL(e.name).pathname,start:e.startTime,end:e.responseEnd}))""")
                                        asset = next(e for e in entries if e['path'] == '/route-assets/benefits-ui.js')
                                        catalog = next(e for e in entries if e['path'] == '/api/route/catalog')
                                        assert catalog['start'] >= asset['end'], (asset, catalog)
                                        timings.append({'navigation': navigation, 'delayed_asset': asset,
                                                        'catalog': catalog})
                                        assert api(f"/api/route/journeys/{state['id']}")['wallet'] == state['wallet']
                                    assert not errors, errors
                                    if n == 1:
                                        page.screenshot(path=str(output / f'{mode}-reload-wallet.png'), full_page=True)
                                    reports.append({'pass': n, 'viewport': mode, 'result': 'passed',
                                        'mode': 'actual_http_delayed_script', 'asset_delay_seconds': 1.5,
                                        'unchanged_server_wallet': True, 'page_errors': errors, 'timings': timings})
                                    (output / 'bootstrap-results.json').write_text(json.dumps(reports, ensure_ascii=False, indent=2))
                                except Exception as error:
                                    page.screenshot(path=str(output / f'{mode}-{n}-failure.png'), full_page=True)
                                    (output / f'{mode}-{n}-failure.json').write_text(json.dumps({
                                        'error': str(error), 'page_errors': errors, 'body': page.locator('body').inner_text(),
                                        'server_state': api(f"/api/route/journeys/{state['id']}")}, ensure_ascii=False, indent=2))
                                    raise
                                finally:
                                    context.close()
                    finally:
                        browser.close()
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    return reports


if __name__ == '__main__':
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--serve', type=int)
    parser.add_argument('--repeat', type=int, default=3)
    parser.add_argument('--output', type=Path, default=Path('verification/bootstrap-browser'))
    args = parser.parse_args()
    if args.serve:
        serve(args.serve)
    else:
        assert 1 <= args.repeat <= 10
        print(json.dumps(verify(args.output, args.repeat), ensure_ascii=False))
