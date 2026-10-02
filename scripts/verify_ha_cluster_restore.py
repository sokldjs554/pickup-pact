#!/usr/bin/env python3
"""Whole-cluster loss and restore from the external store, on the rendered topology.

Development rehearsal on ONE machine (Docker "hosts", a self-created HTTPS
append-only Restic store); never off-host DR evidence. Each repetition:

  1. builds a fresh 3-member Patroni cluster and finishes one order;
  2. starts a second order, moves it to another store, then stops every worker
     and loses the capture reply at pickup, so the capture is stored by the
     synthetic PG but the order still waits on its ORIGINAL operation key;
  3. takes a base backup from a separate backup-host container, sets a named
     restore point and then creates a third order that must NOT come back;
  4. seals base backup + required WAL, uploads them (read-back verified) and
     keeps the receipt and seal digest outside the cluster;
  5. destroys every cluster container and volume (data, WAL spool, etcd) and
     the local bundle, so only the external store remains (DR-01);
  6. downloads the pinned snapshot, rebuilds a NEW cluster from it with Patroni
     custom bootstrap, rotates database passwords and internal tokens, bumps the
     operating generation and starts the services on the new generation;
  7. checks the completed order, resumes the pending one with its original key
     (one 3,200 KRW capture, no hold, 1,000 points spent, 32 earned; DR-05),
     confirms the post-target order is absent (DR-02), refuses the previous
     token and generation (DR-06), and runs the customer/merchant/payment/benefit
     browser suites against an app joined to the restored cluster.

Reported seconds are rehearsal timings, not production RTO/RPO.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ha_cluster_rehearsal import HOSTS, START_ORDER, ClusterRehearsal  # noqa: E402
from ha_restic_lab import ResticLab  # noqa: E402
from verify_ha_cluster import Api, identity  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCOPE = 'single_host_docker_restore_rehearsal_not_off_host_dr'
BROWSER_SUITES = [('route', 'verify_route_browser.py'), ('handoff', 'verify_handoff_browser.py'),
                  ('payment', 'verify_payment_browser.py'), ('benefits', 'verify_benefits_browser.py')]


def progress_to_pickup(api, letter, state):
    minutes = max(0, state['current_plan']['start_at']-state['clock'])
    if minutes:
        state = api.command(letter, state, 'advance', minutes=minutes)
    state = api.command(letter, state, 'start')
    state = api.command(letter, state, 'advance', minutes=max(0, state['order']['ready_at']-state['clock']))
    return api.command(letter, state, 'ready')


def run_browsers(cluster, output, base_url, commit):
    results = []
    for name, script in BROWSER_SUITES:
        log = output/f'browser-{name}.log'
        with log.open('w') as handle:
            try:
                code = subprocess.run([sys.executable, f'scripts/{script}', '--base-url', base_url, '--repeat', '1',
                                       '--expected-commit', commit, '--output', str(output/'browser'/name)],
                                      cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT, timeout=900).returncode
            except subprocess.TimeoutExpired:
                code = 124
        log.write_text(cluster.redact(log.read_text(errors='replace')))
        results.append(dict(suite=name, exit_code=code, passed=code == 0))
    return results


def wait_http(url, timeout=90):
    from urllib.request import urlopen
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(url+'/ready', timeout=5) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(.5)
    raise AssertionError('browser-facing app did not become ready')


def rehearse(output: Path, *, browsers: bool) -> dict:
    report = dict(steps=[])
    cluster = ClusterRehearsal(output)
    host_app = None
    try:
        with ResticLab() as lab:
            began = time.monotonic()
            cluster.setup()
            report['bring_up_s'] = round(time.monotonic()-began, 1)
            api = Api(cluster)

            first = api.start('a')
            completed = api.finish('a', 'b', first)
            report['completed_before_backup'] = completed
            pending = api.settle('a', api.start('a', fault='capture_reply_lost'))
            oat = next(plan for plan in pending['all_plans'] if plan['store_id'] == 'oat')
            pending = api.settle('b', api.command('b', pending, 'transfer', quote_id=oat['quote_id']))
            report['steps'].append('second order reserved and moved to another store')

            base = cluster.base_backup()
            report['base_backup'] = base
            for letter in HOSTS:
                cluster.stop_part(letter, 'worker', kill=False)
            pending = progress_to_pickup(api, 'c', pending)
            pending = api.command('c', pending, 'claim', pickup_code=pending['order']['pickup_code'])
            assert pending['handoff_pending'], 'capture outcome was not left pending'
            report['steps'].append('capture stored by the synthetic PG, reply lost, every worker stopped')
            target = cluster.restore_point('pact_target_'+uuid4().hex[:12])
            later = api.call('a', '/api/route/journeys', {})
            target_at = time.monotonic()
            report['steps'].append('named restore point set; an order created afterwards must not return')

            with tempfile.TemporaryDirectory(prefix='pickup-cluster-bundle-') as staging:
                bundle = Path(staging)/'bundle'
                seal, metadata = cluster.collect_bundle(bundle, base=base, target=target)
                started = time.monotonic()
                receipt = lab.archive.upload(bundle, expected_sha256=seal, expected_cluster_id=metadata['cluster_id'])
                report['upload_and_readback_s'] = round(time.monotonic()-started, 1)
            assert not bundle.exists()
            vault = dict(receipt=receipt, seal=seal, cluster_id=metadata['cluster_id'])
            (output/'receipt.json').write_text(json.dumps(dict(receipt=receipt, seal_sha256=seal), indent=2))
            report.update(required_wal=metadata['required_wal'], target=target['target'], cluster_id=metadata['cluster_id'])

            destroyed_at = time.monotonic()
            errors = cluster.destroy(phase='logs-before-loss')
            assert not errors, errors
            leftovers = subprocess.run(['docker', 'ps', '-aq', '--filter', f'label=pickup.ha-rehearsal={cluster.run}'],
                                       capture_output=True, text=True).stdout.split()
            volumes = subprocess.run(['docker', 'volume', 'ls', '-q', '--filter', f'label=pickup.ha-rehearsal={cluster.run}'],
                                     capture_output=True, text=True).stdout.split()
            assert not leftovers and not volumes, 'cluster resources survived the loss'
            report['steps'].append('all cluster containers, data/WAL-spool/etcd volumes and the local bundle removed')
            report['loss_after_target_s'] = round(destroyed_at-target_at, 1)

            with tempfile.TemporaryDirectory(prefix='pickup-cluster-restore-') as downloaded:
                fetched = Path(downloaded)/'bundle'
                lab.archive.restore(vault['receipt'], fetched, expected_sha256=vault['seal'],
                                    expected_cluster_id=vault['cluster_id'])
                report['downloaded_s'] = round(time.monotonic()-destroyed_at, 1)
                bumped = cluster.setup_restored(fetched, target=target['target'])
            report['generation_bump'] = bumped
            report['apps_ready_after_loss_s'] = round(time.monotonic()-destroyed_at, 1)
            members = cluster.members()
            report['restored_members'] = [(m['Member'], m['Role'], m['State'], m['TL']) for m in members]
            assert all(m['TL'] >= 2 for m in members if m['Role'] == 'Leader')

            restored = api.call('b', '/api/route/journeys/'+first['id'])
            assert api.proof('a', restored) == completed, 'completed order differs after restore'
            report['completed_order_restored'] = True
            missing = api.http.get(cluster.app_url('a')+'/api/route/journeys/'+later['id'])
            assert missing.status_code == 404, missing.status_code
            report['post_target_order_absent'] = True
            resumed = api.settle('b', api.call('b', '/api/route/journeys/'+pending['id']), timeout=120)
            report['resumed_with_original_key'] = api.proof('c', resumed)
            assert report['resumed_with_original_key']['capture_count'] == 1

            from demo.route.merchant_http import HttpMerchantFleet
            merchant = f'https://{cluster.host("a").address}:8443'
            refused = []
            for label, generation in [('previous_generation', 1)]:
                try:
                    HttpMerchantFleet(merchant, cluster.tokens['ROUTE_MERCHANT_TOKEN'], 2,
                                      transport=cluster.internal_transport(generation=generation)).snapshot('probe')
                except OSError:
                    refused.append(label)
            assert refused == ['previous_generation'] and HttpMerchantFleet(
                merchant, cluster.tokens['ROUTE_MERCHANT_TOKEN'], 2,
                transport=cluster.internal_transport(generation=2)).snapshot('probe')
            report['refused_after_restore'] = refused

            if browsers:
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1', 0))
                    port = sock.getsockname()[1]
                host_app = cluster.host_app(port)
                base_url = f'http://127.0.0.1:{port}'
                wait_http(base_url)
                runtime = json.loads(subprocess.check_output(['curl', '-sf', base_url+'/api/route/runtime'], text=True))
                assert runtime['storage_backend'] == 'postgresql' and runtime['scope'].startswith('ha_postgres_v1')
                report['browser_results'] = run_browsers(cluster, output, base_url, os.environ['RENDER_GIT_COMMIT'])
                assert all(row['passed'] for row in report['browser_results']), report['browser_results']
            report['passed'] = True
            return report
    finally:
        if host_app is not None:
            process, log = host_app
            process.terminate()
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                process.kill()
            log.close()
            path = cluster.evidence/'host-app.log'
            path.write_text(cluster.redact(path.read_text(errors='replace')))
        cluster.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeat', type=int, default=3, choices=range(1, 6))
    parser.add_argument('--browsers', action='store_true')
    args = parser.parse_args()
    if os.environ.get('PICKUP_HA_TEST') != '1':
        raise SystemExit('explicit isolated rehearsal opt-in required (PICKUP_HA_TEST=1)')
    if args.browsers and not os.environ.get('RENDER_GIT_COMMIT'):
        raise SystemExit('RENDER_GIT_COMMIT must name the tested commit for the browser suites')
    args.output.mkdir(parents=True, exist_ok=False)
    report = dict(scope=SCOPE, timings='single-host rehearsal timings, not production RTO/RPO',
                  identity=identity(), repetitions=[], passed=False)
    failures = 0
    for number in range(1, args.repeat+1):
        row = dict(repeat=number)
        try:
            row.update(rehearse(args.output/f'run-{number}', browsers=args.browsers))
        except Exception as exc:  # noqa: BLE001 - failed iterations are retained
            failures += 1
            row.update(passed=False, error=f'{type(exc).__name__}: {str(exc)[:1500]}')
        report['repetitions'].append(row)
        (args.output/'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps({'repeat': number, 'passed': row.get('passed', False),
                          'apps_ready_after_loss_s': row.get('apps_ready_after_loss_s')}), flush=True)
    report['passed'] = failures == 0
    (args.output/'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'passed': report['passed'], 'failures': failures, 'scope': SCOPE}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
