#!/usr/bin/env python3
"""Failover scenarios on the single-host Patroni rehearsal (development evidence only).

Each repetition builds a NEW cluster from the rendered inventory and runs, in
order and behind a convergence gate (3 members, a streaming synchronous standby,
every app ready):

  WAL   the rendered archive_command spools WAL on the leader
  HA-01 the same request through two app instances
  HA-02 payment reply lost, then that app host's API and worker stop
  HA-03 merchant and payment roles of one host stop during an order
  HA-04 the database leader host is killed during an order
  HA-05 the synchronous standby, then every standby, stops (strict sync must block)
  HA-06 the leader host loses the cluster network but stays reachable by clients
  DR-06 (generation step only) bump the operating generation on the live cluster;
        previous-generation processes stop writing, the order resumes on the new one

An order that reaches "picked up" must end with one 3,200 KRW capture, no held
authorization, 1,000 points spent and 32 earned. During database faults a probe
records every acknowledged write; all of them must exist afterwards. Writes that
timed out are reported as unknown outcomes, not as successes or losses.

All "hosts" are containers on one machine: this is not independent-host HA,
and the measured seconds are rehearsal timings, not production RTO/RPO.
HA-07..HA-09 are covered by the native PostgreSQL test suite; HA-10 needs real
entry hosts and is not rehearsed here.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ha_cluster_rehearsal import HOSTS, ClusterRehearsal  # noqa: E402

SCOPE = 'single_host_docker_rehearsal_not_host_ha'


class Api:
    def __init__(self, cluster: ClusterRehearsal):
        self.cluster = cluster
        self.http = cluster.client(timeout=15)

    def call(self, letter, path, body=None):
        url = self.cluster.app_url(letter)+path
        response = self.http.get(url) if body is None else self.http.post(url, json=body)
        if response.status_code >= 400:
            raise AssertionError(f'{path} -> {response.status_code}: {response.text[:300]}')
        return response.json()

    def command(self, letter, state, action, request_id=None, **extra):
        return self.call(letter, '/api/route/journeys/'+state['id']+'/commands',
                         dict(action=action, expected_version=state['version'], request_id=request_id or uuid4().hex, **extra))

    def settle(self, letter, state, timeout=60):
        deadline = time.monotonic()+timeout
        while state['handoff_pending'] and time.monotonic() < deadline:
            time.sleep(.25)
            try:
                state = self.call(letter, '/api/route/journeys/'+state['id'])
            except Exception:  # noqa: BLE001 - the app may be failing over
                pass
        assert not state['handoff_pending'], 'automatic recovery did not finish'
        return state

    def start(self, letter, fault='none'):
        state = self.call(letter, '/api/route/journeys', {'coupon_id': 'welcome500', 'points': 1000,
                                                          'payment_scenario': fault})
        wave = next(plan for plan in state['all_plans'] if plan['store_id'] == 'wave')
        return self.command(letter, state, 'reserve', quote_id=wave['quote_id'])

    def finish(self, first, second, state):
        state = self.settle(first, state)
        oid = state['order']['id']
        oat = next(plan for plan in state['all_plans'] if plan['store_id'] == 'oat')
        state = self.settle(second, self.retry(lambda: self.command(second, state, 'transfer', quote_id=oat['quote_id'])))
        assert state['order']['id'] == oid and state['order']['price'] == 3200
        minutes = max(0, state['current_plan']['start_at']-state['clock'])
        if minutes:
            state = self.command(first, state, 'advance', minutes=minutes)
        state = self.command(second, state, 'start')
        state = self.command(first, state, 'advance', minutes=max(0, state['order']['ready_at']-state['clock']))
        state = self.command(second, state, 'ready')
        state = self.settle(second, self.command(first, state, 'claim', pickup_code=state['order']['pickup_code']))
        return self.proof(second, state)

    def retry(self, action, timeout=150):
        deadline = time.monotonic()+timeout
        while True:
            try:
                return action()
            except Exception:  # noqa: BLE001 - storage may be failing over
                if time.monotonic() > deadline:
                    raise
                time.sleep(1)

    def proof(self, letter, state):
        proof = self.call(letter, '/api/route/journeys/'+state['id']+'/reconciliation')
        assert proof['status'] == 'MATCH' and proof['terminal'], proof['status']
        payment = proof['payment']
        assert payment['capture_count'] == 1 and payment['captured_krw'] == 3200 and payment['held_krw'] == 0, payment
        assert state['wallet']['spent'] == 1000 and state['wallet']['earned'] == 32, state['wallet']
        assert proof['merchants']['wave']['reservation']['phase'] == 'RELEASED'
        assert proof['merchants']['oat']['reservation']['phase'] == 'CLAIMED'
        return dict(order_id=state['order']['id'], captured_krw=payment['captured_krw'],
                    capture_count=payment['capture_count'], held_krw=payment['held_krw'],
                    points_spent=state['wallet']['spent'], points_earned=state['wallet']['earned'])


class WriteProbe(threading.Thread):
    """Creates journeys through one app; remembers what was acknowledged."""

    def __init__(self, cluster, letter, interval=.25):
        super().__init__(daemon=True)
        self.cluster, self.letter, self.interval = cluster, letter, interval
        self.events, self.stop_event = [], threading.Event()

    def run(self):
        http = self.cluster.client(timeout=6)
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                response = http.post(self.cluster.app_url(self.letter)+'/api/route/journeys', json={})
                ok = response.status_code == 201
                self.events.append(dict(t=started, ok=ok, id=response.json().get('id') if ok else None,
                                        status=response.status_code))
            except Exception as exc:  # noqa: BLE001 - timeouts are unknown outcomes
                self.events.append(dict(t=started, ok=False, id=None, status=type(exc).__name__))
            self.stop_event.wait(self.interval)

    def stop(self):
        self.stop_event.set()
        self.join(15)

    def acknowledged(self):
        return [event['id'] for event in self.events if event['ok']]

    def gap_after(self, since):
        """Longest stretch without an acknowledged write that starts after `since`."""
        acks = [event['t'] for event in self.events if event['ok'] and event['t'] >= since]
        last = [event['t'] for event in self.events if event['ok'] and event['t'] < since]
        points = ([last[-1]] if last else [since]) + acks
        return max((b-a for a, b in zip(points, points[1:])), default=None) if len(points) > 1 else None

    def first_ack_after(self, since):
        return next((event['t']-since for event in self.events if event['ok'] and event['t'] >= since), None)

    def acks_between(self, start, end):
        return sum(1 for event in self.events if event['ok'] and start <= event['t'] <= end)


class LeaderSampler(threading.Thread):
    """Asks every member, over the client network, whether it accepts writes."""

    def __init__(self, cluster):
        super().__init__(daemon=True)
        self.cluster, self.samples, self.stop_event = cluster, [], threading.Event()

    def run(self):
        import psycopg
        while not self.stop_event.is_set():
            writable, standby_names = [], {}
            for letter in HOSTS:
                try:
                    with psycopg.connect(self.cluster.runtime_dsn('order', member=letter, attrs='any'), autocommit=True) as db:
                        row = db.execute("SELECT pg_is_in_recovery(), current_setting('transaction_read_only'), "
                                         "current_setting('synchronous_standby_names')").fetchone()
                        if not row[0] and row[1] == 'off':
                            writable.append(letter)
                            standby_names[letter] = row[2]
                except psycopg.Error:
                    pass
            self.samples.append(dict(t=time.monotonic(), writable=writable, synchronous_standby_names=standby_names))
            self.stop_event.wait(.5)

    def stop(self):
        self.stop_event.set()
        self.join(20)

    def overlaps(self):
        return sum(1 for sample in self.samples if len(sample['writable']) > 1)


def verify_acknowledged(api, letter, ids):
    missing = []
    for journey_id in ids:
        try:
            api.call(letter, '/api/route/journeys/'+journey_id)
        except AssertionError:
            missing.append(journey_id)
    return missing


def others(*excluded):
    return [letter for letter in HOSTS if letter not in excluded]


def ha01(cluster, api):
    state = api.start('a')
    oat = next(plan for plan in state['all_plans'] if plan['store_id'] == 'oat')
    body = dict(action='transfer', expected_version=state['version'], request_id='same-'+uuid4().hex, quote_id=oat['quote_id'])
    first = api.call('a', '/api/route/journeys/'+state['id']+'/commands', body)
    second = api.call('b', '/api/route/journeys/'+state['id']+'/commands', body)
    assert second['duplicate'] and second['order'] == first['order'] and second['handoff'] == first['handoff']
    conflict = api.http.post(cluster.app_url('c')+'/api/route/journeys/'+state['id']+'/commands',
                             json=body | {'quote_id': 'changed'})
    assert conflict.status_code == 409, conflict.status_code
    state = api.settle('b', first)
    # Continue the same order to the end through the third instance.
    minutes = max(0, state['current_plan']['start_at']-state['clock'])
    if minutes:
        state = api.command('c', state, 'advance', minutes=minutes)
    state = api.command('a', state, 'start')
    state = api.command('b', state, 'advance', minutes=max(0, state['order']['ready_at']-state['clock']))
    state = api.command('c', state, 'ready')
    state = api.settle('a', api.command('b', state, 'claim', pickup_code=state['order']['pickup_code']))
    return dict(proof=api.proof('c', state), conflict_status=409)


def ha02(cluster, api):
    """The capture is stored but its reply is lost; then host a's API and worker stop."""
    lost = api.settle('a', api.start('a', fault='capture_reply_lost'))
    oat = next(plan for plan in lost['all_plans'] if plan['store_id'] == 'oat')
    lost = api.settle('a', api.command('a', lost, 'transfer', quote_id=oat['quote_id']))
    minutes = max(0, lost['current_plan']['start_at']-lost['clock'])
    if minutes:
        lost = api.command('a', lost, 'advance', minutes=minutes)
    lost = api.command('a', lost, 'start')
    lost = api.command('a', lost, 'advance', minutes=max(0, lost['order']['ready_at']-lost['clock']))
    lost = api.command('a', lost, 'ready')
    lost = api.command('a', lost, 'claim', pickup_code=lost['order']['pickup_code'])
    assert lost['handoff_pending'], 'capture reply loss was not left pending'
    cluster.stop_part('a', 'app')
    cluster.stop_part('a', 'worker')
    try:
        lost = api.settle('b', lost, timeout=90)
        return dict(stopped=['pact-a app', 'pact-a worker'], proof=api.proof('c', lost))
    finally:
        cluster.start_part('a', 'app')
        cluster.start_part('a', 'worker')
        cluster.wait_apps(HOSTS)


def ha03(cluster, api):
    cluster.stop_part('a', 'merchant')
    cluster.stop_part('a', 'payment')
    try:
        state = api.start('a')
        return dict(stopped=['pact-a merchant', 'pact-a payment'], proof=api.finish('a', 'a', state))
    finally:
        cluster.start_part('a', 'merchant')
        cluster.start_part('a', 'payment')
        cluster.wait_apps(HOSTS)


def database_fault(cluster, api, inject, recover, *, label):
    leader = cluster.leader()
    probe_host = others(leader)[0]
    order_host = others(leader)[-1]
    state = api.start(order_host)
    sampler = LeaderSampler(cluster)
    probe = WriteProbe(cluster, probe_host)
    sampler.start()
    probe.start()
    time.sleep(3)
    details = inject(leader)
    started = time.monotonic()  # the fault has been delivered
    proof = api.finish(order_host, probe_host, state)
    details.update(recover(leader, started, probe, sampler) or {})
    probe.stop()
    sampler.stop()
    acknowledged = probe.acknowledged()
    missing = verify_acknowledged(api, probe_host, acknowledged)
    unknown = sum(1 for event in probe.events if not event['ok'] and event['status'] not in {400, 422})
    assert not missing, f'{len(missing)} acknowledged writes missing after {label}'
    assert sampler.overlaps() == 0, 'two writable database members observed'
    return dict(leader_before=leader, leader_after=cluster.leader(), proof=proof,
                acknowledged_writes=len(acknowledged), missing_acknowledged=len(missing),
                unknown_outcomes=unknown, writable_overlap_samples=sampler.overlaps(),
                first_write_after_fault_s=_round(probe.first_ack_after(started)),
                longest_write_gap_s=_round(probe.gap_after(started)),
                members=cluster.members(), **details)


def _round(value):
    return None if value is None else round(value, 2)


def ha04(cluster, api):
    def inject(leader):
        cluster.kill_host(leader)
        return {'killed_host': 'pact-'+leader}

    def recover(leader, started, probe, sampler):
        cluster.wait_cluster(members=2, exclude=(leader,), via=others(leader)[0])
        promoted = time.monotonic()-started
        cluster.start_host(leader)
        cluster.wait_cluster(members=3)
        cluster.wait_apps(HOSTS)
        time.sleep(2)
        rejoined = next(m for m in cluster.members() if m['Member'] == 'pact-'+leader)
        assert rejoined['Role'] in {'Replica', 'Sync Standby'} and rejoined['State'] == 'streaming'
        return {'new_leader_observed_s': round(promoted, 2), 'old_leader_rejoined_as': rejoined['Role']}
    return database_fault(cluster, api, inject, recover, label='HA-04')


def ha05(cluster, api):
    members = cluster.wait_cluster(members=3)
    leader = next(m['Member'][-1] for m in members if m['Role'] == 'Leader')
    sync = next(m['Member'][-1] for m in members if m['Role'] == 'Sync Standby')
    replica = next(m['Member'][-1] for m in members if m['Role'] == 'Replica')
    probe_host = leader  # its app stays up; only standby database processes stop
    probe = WriteProbe(cluster, probe_host)
    sampler = LeaderSampler(cluster)
    sampler.start()
    probe.start()
    time.sleep(3)
    started = time.monotonic()
    cluster.stop_part(sync, 'patroni')
    cluster.wait_cluster(members=2, exclude=(sync,), via=leader, timeout=120)
    resynced = time.monotonic()-started
    time.sleep(3)
    cluster.stop_part(replica, 'patroni')
    blocked_from = time.monotonic()+8  # commits already in flight may still finish
    time.sleep(28)
    blocked_until = time.monotonic()
    acks_while_no_standby = probe.acks_between(blocked_from, blocked_until)
    strict = [sample['synchronous_standby_names'].get(leader) for sample in sampler.samples
              if blocked_from <= sample['t'] <= blocked_until and leader in sample['writable']]
    cluster.start_part(sync, 'patroni')
    restarted = time.monotonic()
    cluster.wait_cluster(members=2, exclude=(replica,), via=leader, timeout=120)
    resumed = probe.first_ack_after(restarted)
    cluster.start_part(replica, 'patroni')
    cluster.wait_cluster(members=3)
    probe.stop()
    sampler.stop()
    acknowledged = probe.acknowledged()
    missing = verify_acknowledged(api, probe_host, acknowledged)
    assert acks_while_no_standby == 0, f'{acks_while_no_standby} writes acknowledged without a synchronous standby'
    assert not missing and sampler.overlaps() == 0
    assert strict and all(value == '*' for value in strict), f'strict synchronous mode relaxed: {strict}'
    state = api.start(others(leader)[0])
    return dict(leader=leader, stopped_sync_standby=sync, stopped_replica=replica,
                new_sync_standby_observed_s=round(resynced, 2), acks_with_no_standby=acks_while_no_standby,
                strict_standby_names=sorted(set(strict)), writes_resumed_after_standby_s=_round(resumed),
                acknowledged_writes=len(acknowledged), missing_acknowledged=len(missing),
                unknown_outcomes=sum(1 for event in probe.events if not event['ok']),
                proof=api.finish(others(leader)[0], leader, state), members=cluster.members())


def ha06(cluster, api):
    def inject(leader):
        cluster.partition(leader)
        return {'partitioned_host': 'pact-'+leader}

    def recover(leader, started, probe, sampler):
        cluster.wait_cluster(members=2, exclude=(leader,), via=others(leader)[0], timeout=180)
        promoted = time.monotonic()-started
        cluster.heal(leader)
        cluster.wait_cluster(members=3, timeout=240)
        cluster.wait_apps(HOSTS)
        rejoined = next(m for m in cluster.members() if m['Member'] == 'pact-'+leader)
        old_writable_after_promotion = [s for s in sampler.samples if s['t'] > started+promoted and leader in s['writable']]
        assert not old_writable_after_promotion, 'old leader still writable after a new leader was elected'
        return {'new_leader_observed_s': round(promoted, 2), 'old_leader_rejoined_as': rejoined['Role']}
    return database_fault(cluster, api, inject, recover, label='HA-06')


def generation_fence(cluster, api):
    """Post-restore step on a live cluster: bump the operating generation, then roll forward.

    Processes configured for the previous generation must stop acknowledging
    writes; the in-flight order then finishes on the new generation with its
    ORIGINAL request and operation keys (no second capture).
    """
    state = api.settle('a', api.start('a'))
    probe = WriteProbe(cluster, 'b')
    probe.start()
    time.sleep(2)
    bumps = cluster.migrate('bump-generation', '--expected', '1', '--reason', 'rehearsal-'+cluster.run)
    bumped = time.monotonic()
    time.sleep(12)
    stale_acks = probe.acks_between(bumped+1, time.monotonic())
    readiness = {}
    with cluster.client(timeout=15) as client:
        for letter in HOSTS:
            readiness[letter] = client.get(cluster.app_url(letter)+'/ready').status_code
    probe.stop()
    assert all(item['generation'] == 2 and item['changed'] for item in bumps), bumps
    assert stale_acks == 0, f'{stale_acks} writes acknowledged by previous-generation processes'
    assert set(readiness.values()) == {503}, readiness
    again = cluster.migrate('bump-generation', '--expected', '1', '--reason', 'rehearsal-'+cluster.run)
    assert not any(item['changed'] for item in again), 'the same restore reason advanced twice'
    previous = cluster.rotate_tokens()
    cluster.roll_generation(2)
    cluster.wait_apps(HOSTS, timeout=180)
    from demo.route.merchant_http import HttpMerchantFleet
    merchant = f'https://{cluster.host("a").address}:8443'
    current = HttpMerchantFleet(merchant, cluster.tokens['ROUTE_MERCHANT_TOKEN'], 2,
                                transport=cluster.internal_transport(generation=2))
    assert current.snapshot('generation-probe')
    refused = {}
    for label, token, generation in [('previous_token', previous['ROUTE_MERCHANT_TOKEN'], 2),
                                     ('previous_generation', cluster.tokens['ROUTE_MERCHANT_TOKEN'], 1)]:
        try:
            HttpMerchantFleet(merchant, token, 2, transport=cluster.internal_transport(generation=generation)) \
                .snapshot('generation-probe')
        except OSError:
            refused[label] = True
    assert refused == {'previous_token': True, 'previous_generation': True}, refused
    runtime = api.call('b', '/api/route/runtime')
    proof = api.finish('b', 'c', state)
    return dict(generation_after=2, stale_generation_acks=stale_acks, readiness_after_bump=readiness,
                repeated_bump_changed=False, tokens_rotated=True, refused_after_roll=sorted(refused),
                runtime_scope=runtime['scope'], proof=proof, acknowledged_before_bump=len(probe.acknowledged()))


def wal_archive(cluster, api):
    """The rendered archive_command spools WAL on the Patroni leader without failures."""
    del api
    leader = cluster.leader()
    before = cluster.psql_leader('SELECT archived_count FROM pg_stat_archiver;', options=('-At',)).stdout.strip()
    cluster.psql_leader('SELECT pg_switch_wal();')
    deadline = time.monotonic()+60
    row = ''
    while time.monotonic() < deadline:
        row = cluster.psql_leader("SELECT archived_count||' '||failed_count FROM pg_stat_archiver;",
                                  options=('-At',)).stdout.strip()
        if int(row.split()[0]) > int(before or 0):
            break
        time.sleep(1)
    archived, failed = (int(part) for part in row.split())
    spooled = cluster.docker('exec', cluster.name(leader, 'patroni'), 'ls', '/var/lib/pickup-pact/wal-spool').stdout.split()
    assert archived > int(before or 0) and failed == 0, row
    from demo.route.ha.backup_ops import archived_name
    assert spooled and all(archived_name(name) for name in spooled), spooled
    return dict(leader=leader, archived_count=archived, failed_count=failed, spooled_files=len(spooled))


SCENARIOS = [('WAL-ARCHIVE', wal_archive), ('HA-01', ha01), ('HA-02', ha02), ('HA-03', ha03), ('HA-04', ha04),
             ('HA-05', ha05), ('HA-06', ha06), ('DR-06-generation', generation_fence)]


def identity():
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    dirty = bool(subprocess.run(['git', 'status', '--porcelain'], capture_output=True, text=True).stdout.strip())
    images = {}
    for image in ['pickup-patroni-rehearsal:dev', 'pickup-app-rehearsal:dev', 'gcr.io/etcd-development/etcd:v3.5.17']:
        info = json.loads(subprocess.check_output(['docker', 'image', 'inspect', image], text=True))[0]
        images[image] = {'id': info['Id'], 'repo_digests': info.get('RepoDigests', [])}
    return dict(commit=commit, worktree_dirty=dirty, images=images)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeat', type=int, default=3, choices=range(1, 6))
    parser.add_argument('--only', nargs='*', default=None)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    report = dict(scope=SCOPE, timings='single-host rehearsal timings, not production RTO/RPO',
                  not_rehearsed={'HA-07..HA-09': 'native PostgreSQL pytest suite', 'HA-10': 'needs independent entry hosts'},
                  identity=identity(), repetitions=[], passed=False)
    failures = 0
    for number in range(1, args.repeat+1):
        cluster = ClusterRehearsal(args.output/f'run-{number}')
        repetition = dict(repeat=number, run_id=cluster.run, scenarios=[])
        report['repetitions'].append(repetition)
        try:
            began = time.monotonic()
            cluster.setup()
            repetition['bring_up_s'] = round(time.monotonic()-began, 1)
            repetition['render_manifest'] = cluster.manifest
            api = Api(cluster)
            for name, scenario in SCENARIOS:
                if args.only and name not in args.only:
                    continue
                began = time.monotonic()
                row = dict(id=name)
                try:
                    cluster.wait_cluster(members=3)
                    cluster.wait_apps(HOSTS)
                    row.update(scenario(cluster, api))
                    row['passed'] = True
                except Exception as exc:  # noqa: BLE001 - every failure is retained
                    failures += 1
                    row.update(passed=False, error=cluster.redact(f'{type(exc).__name__}: {exc}')[:1500])
                row['elapsed_s'] = round(time.monotonic()-began, 1)
                repetition['scenarios'].append(row)
                print(json.dumps({'repeat': number, 'scenario': name, 'passed': row['passed'],
                                  'elapsed_s': row['elapsed_s']}), flush=True)
        except Exception as exc:  # noqa: BLE001
            failures += 1
            repetition['setup_error'] = cluster.redact(f'{type(exc).__name__}: {exc}')[:1500]
        finally:
            try:
                cluster.close()
            except Exception as exc:  # noqa: BLE001
                failures += 1
                repetition['cleanup_error'] = str(exc)[:500]
        (args.output/'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    report['passed'] = failures == 0 and all(row['passed'] for rep in report['repetitions'] for row in rep['scenarios'])
    (args.output/'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'passed': report['passed'], 'failures': failures, 'scope': SCOPE}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    if os.environ.get('PICKUP_HA_TEST') != '1':
        raise SystemExit('explicit isolated rehearsal opt-in required (PICKUP_HA_TEST=1)')
    raise SystemExit(main())
