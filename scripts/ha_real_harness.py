#!/usr/bin/env python3
"""독립 호스트 장애 시험 도구. DB 클러스터 밖의 pact-backup에서 실행한다(승인된 시험 서버 전용).

기존 `verify_ha_cluster.py`의 시나리오(HA-01~06, WAL)를 그대로 쓰고, Docker 대신 SSH로 장애를 주입한다.
  - 호스트 장애(HA-04): 커널을 즉시 재부팅(sysrq 'b')한다. 디스크 동기화·정상 종료 없이 멈추는, 전원 차단과 같은 효과다.
  - 망 분리(HA-06): 그 호스트와 다른 DB 호스트 사이의 트래픽만 서버 방화벽으로 끊는다. 시험 도구(10.0.0.20)와 SSH는 유지된다.
  - 서비스 중단(HA-02/03/05): systemd 유닛을 멈춘다.
임시 SSH 키는 DB 서버에 `from=10.0.0.20`으로 제한되어 시험 동안만 등록된다.
비밀값은 출력·결과에 쓰지 않는다. 측정 시간은 시험 환경의 값이지 운영 RTO/RPO가 아니다.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shlex
import ssl
import threading
import subprocess
import sys
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_ha_cluster as base  # noqa: E402
from verify_ha_cluster import HOSTS  # noqa: E402

SCOPE = 'oracle_single_ad_fault_domains'
ADDRESSES = {'a': '10.0.0.11', 'b': '10.0.0.12', 'c': '10.0.0.13'}
UNITS = {'app': 'pickup-order-app', 'worker': 'pickup-order-worker', 'merchant': 'pickup-merchant',
         'payment': 'pickup-payment', 'notification': 'pickup-order-notification', 'patroni': 'patroni', 'etcd': 'etcd'}
ALL_UNITS = ['etcd', 'patroni', 'pickup-merchant', 'pickup-order-notification', 'pickup-payment',
             'pickup-order-app', 'pickup-order-worker']
PATRONICTL = 'sudo -u postgres /opt/patroni/bin/patronictl -c /etc/pickup-pact/patroni.yml'


class RealCluster:
    def __init__(self, workdir: Path):
        self.work = Path(workdir)
        self.key = self.work/'id'
        self.known = self.work/'known_hosts'
        self.ca = self.work/'ca.crt'
        self.cert, self.cert_key = self.work/'harness.crt', self.work/'harness.key'
        self.pgpass = self.work/'order-runtime.pgpass'
        self.run = uuid4().hex[:12]

    # ---------------------------------------------------------------- SSH
    def _ssh(self, letter) -> list[str]:
        return ['ssh', '-i', str(self.key), '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
                '-o', 'StrictHostKeyChecking=accept-new', '-o', f'UserKnownHostsFile={self.known}',
                '-o', 'ConnectTimeout=6', '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=2',
                '-o', 'ControlMaster=auto', '-o', f'ControlPath={self.work}/cm-%r@%h', '-o', 'ControlPersist=120',
                f'ubuntu@{ADDRESSES[letter]}']

    def sh(self, letter, command: str, *, timeout=60, check=True) -> subprocess.CompletedProcess:
        done = subprocess.run(self._ssh(letter)+['bash', '-lc', shlex.quote(command)], capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL)
        if check and done.returncode:
            raise RuntimeError(f'pact-{letter}: exit {done.returncode}: {done.stderr.strip()[-300:]}')
        return done

    def redact(self, text: str) -> str:
        return text

    # ---------------------------------------------------------------- HTTP / SQL 접속
    def client(self, timeout=20):
        import httpx
        context = ssl.create_default_context(cafile=str(self.ca))
        context.load_cert_chain(str(self.cert), str(self.cert_key))
        return httpx.Client(verify=context, timeout=timeout, trust_env=False, follow_redirects=False)

    def app_url(self, letter):
        return f'https://pact-{letter}.pact.internal:8000'

    def runtime_dsn(self, role, *, member=None, attrs='read-write'):
        hosts = f'pact-{member}.pact.internal' if member else ','.join(f'pact-{x}.pact.internal' for x in HOSTS)
        ports = '5432' if member else ','.join('5432' for _ in HOSTS)
        return (f'host={hosts} port={ports} dbname=pact_orders user=pact_order_runtime sslmode=verify-full '
                f'sslrootcert={self.ca} passfile={self.pgpass} target_session_attrs={attrs} connect_timeout=2')

    # ---------------------------------------------------------------- Patroni
    def members(self, via=None) -> list[dict]:
        for letter in ([via] if via else HOSTS):
            try:
                done = self.sh(letter, PATRONICTL+' list -f json', check=False, timeout=40)
            except subprocess.TimeoutExpired:
                continue
            if done.returncode == 0 and done.stdout.strip():
                return json.loads(done.stdout)
        raise RuntimeError('Patroni 구성원 목록을 읽을 수 없다')

    def leader(self, via=None):
        for member in self.members(via):
            if member.get('Role') == 'Leader' and member.get('State') == 'running':
                return member['Member'][-1]
        return None

    def wait_cluster(self, members=3, timeout=180, via=None, exclude=()):
        deadline = time.monotonic()+timeout
        last = None
        while time.monotonic() < deadline:
            try:
                last = self.members(via)
                live = [m for m in last if m['Member'][-1] not in exclude]
                leaders = [m for m in live if m.get('Role') == 'Leader' and m.get('State') == 'running']
                sync = [m for m in live if m.get('Role') == 'Sync Standby' and m.get('State') == 'streaming']
                streaming = [m for m in live if m.get('Role') in {'Sync Standby', 'Replica'} and m.get('State') == 'streaming']
                if len(leaders) == 1 and sync and len(streaming) >= members-1:
                    return last
            except (RuntimeError, ValueError):
                pass
            time.sleep(2)
        raise AssertionError('Patroni 클러스터가 수렴하지 않았다: '+json.dumps(last))

    def wait_apps(self, letters, timeout=180):
        deadline = time.monotonic()+timeout
        pending, last = set(letters), {}
        with self.client(timeout=5) as client:
            while pending and time.monotonic() < deadline:
                for letter in sorted(pending):
                    try:
                        response = client.get(self.app_url(letter)+'/ready')
                        last[letter] = response.status_code
                        if response.status_code == 200:
                            runtime = client.get(self.app_url(letter)+'/api/route/runtime').json()
                            if runtime.get('storage_backend') == 'postgresql' and runtime.get('automatic_recovery'):
                                pending.discard(letter)
                    except Exception as exc:  # noqa: BLE001 - 준비 대기
                        last[letter] = type(exc).__name__
                time.sleep(1)
        if pending:
            raise AssertionError('앱이 준비되지 않았다: '+json.dumps(last))

    # ---------------------------------------------------------------- 장애 주입
    def stop_part(self, letter, part, *, kill=True):
        unit = UNITS[part]
        self.sh(letter, f'sudo systemctl stop {unit}', timeout=90)
        if part == 'patroni':  # Patroni가 PostgreSQL을 남기지 않았는지 확인하고, 남았으면 강제로 끝낸다.
            self.sh(letter, 'sudo pkill -KILL -u postgres -x postgres || true', check=False)

    def start_part(self, letter, part):
        self.sh(letter, f'sudo systemctl start {UNITS[part]}', timeout=120)

    def kill_host(self, letter):
        """호스트를 즉시 크래시시키고(sysrq b: 정상 종료·동기화 없음) 다음 부팅에서 DB 역할(etcd, Patroni)이 올라오지 않게 한다.

        실제 호스트는 임대 TTL(30초)보다 빨리 돌아오면 장애 조치 없이 다시 리더가 된다. 장시간 정지를 모사하려고
        정지 조건 파일을 디스크에 확정한 뒤 크래시시키고, start_host가 시험이 정한 시점에 파일을 지운다.
        """
        # 정지 조건 파일: 있으면 etcd·patroni가 시작되지 않는다(드롭인은 ensure_hold_dropins가 둔다).
        self.sh(letter, 'sudo touch /etc/pickup-pact/hold && sync')
        subprocess.run(self._ssh(letter)+['sudo', 'sh', '-c', shlex.quote('echo 1 > /proc/sys/kernel/sysrq; echo b > /proc/sysrq-trigger')],
                       capture_output=True, timeout=15, stdin=subprocess.DEVNULL)

    def start_host(self, letter, timeout=420):
        """재부팅한 호스트가 SSH에 응답할 때까지 기다린 뒤 DB 역할을 되돌리고 유닛을 시작한다."""
        deadline = time.monotonic()+timeout
        time.sleep(15)  # 크래시가 반영되기 전에 응답하는 옛 연결을 건너뛴다
        while time.monotonic() < deadline:
            try:
                if self.sh(letter, 'true', timeout=12, check=False).returncode == 0:
                    break
            except subprocess.TimeoutExpired:
                pass
            time.sleep(4)
        else:
            raise AssertionError(f'pact-{letter}가 재부팅 후 응답하지 않는다')
        self.sh(letter, 'sudo rm -f /etc/pickup-pact/hold && sync', check=False)
        for unit in ALL_UNITS:
            self.sh(letter, f'systemctl is-active --quiet {unit} || sudo systemctl start {unit}', timeout=150, check=False)

    def partition(self, letter):
        peers = ','.join(ADDRESSES[x] for x in HOSTS if x != letter)
        self.sh(letter, f'sudo iptables -I INPUT 1 -s {peers} -m comment --comment pact-partition -j DROP && '
                        f'sudo iptables -I OUTPUT 1 -d {peers} -m comment --comment pact-partition -j DROP')

    def heal(self, letter):
        """pact-partition 표시가 붙은 규칙만 제거한다(나머지 규칙은 그대로 다시 적용)."""
        self.sh(letter, "sudo iptables-save | grep -v 'pact-partition' | sudo iptables-restore", check=False)

    def ensure_hold_dropins(self):
        """호스트 정지 조건 드롭인: /etc/pickup-pact/hold 파일이 있으면 etcd·patroni 시작을 건너뛴다."""
        for letter in HOSTS:
            self.sh(letter, "sudo rm -f /etc/pickup-pact/hold; for u in etcd patroni; do sudo mkdir -p /etc/systemd/system/$u.service.d && "
                            "printf '[Unit]\\nConditionPathExists=!/etc/pickup-pact/hold\\n' | sudo tee /etc/systemd/system/$u.service.d/hold.conf >/dev/null; done; "
                            "sudo systemctl daemon-reload && sync")

    def remove_hold_dropins(self):
        for letter in HOSTS:
            try:
                self.sh(letter, 'sudo rm -f /etc/pickup-pact/hold /etc/systemd/system/etcd.service.d/hold.conf '
                                '/etc/systemd/system/patroni.service.d/hold.conf && sudo systemctl daemon-reload', check=False)
            except Exception:  # noqa: BLE001
                pass

    def ensure_units(self):
        """정지 조건과 멈춘 유닛을 모두 되돌린다(앞선 시험의 장애가 남지 않게)."""
        for letter in HOSTS:
            try:
                self.sh(letter, 'sudo rm -f /etc/pickup-pact/hold && sync', check=False)
                for unit in ALL_UNITS:
                    self.sh(letter, f'systemctl is-active --quiet {unit} || sudo systemctl start {unit}', timeout=150, check=False)
            except Exception:  # noqa: BLE001
                pass

    def clear_faults(self):
        """분리 규칙과 정지 조건 파일을 지우고, 멈춘 유닛을 시작한다."""
        for letter in HOSTS:
            try:
                self.heal(letter)
                self.sh(letter, 'sudo rm -f /etc/pickup-pact/hold && sync', check=False)
            except Exception:  # noqa: BLE001
                pass
        self.ensure_units()

    def psql_leader(self, sql: str) -> str:
        leader = self.leader()
        done = self.sh(leader, f'sudo -u postgres psql -X -q -At -v ON_ERROR_STOP=1 -c {shlex.quote(sql)}', timeout=60)
        return done.stdout.strip()


class RealApi(base.Api):
    """실서버 장애 조치(약 45초 이상) 동안 주문 복구를 기다릴 수 있도록 대기 시간을 늘린다."""

    def settle(self, letter, state, timeout=60):
        return super().settle(letter, state, timeout=max(timeout, 180))

    def command(self, letter, state, action, request_id=None, **extra):
        """저장소가 장애 조치 중이면 서비스는 503으로 "같은 요청으로 다시 확인"을 안내한다. 같은 요청 키로 다시 보낸다."""
        from uuid import uuid4
        request_id = request_id or uuid4().hex
        deadline = time.monotonic()+180
        while True:
            try:
                return super().command(letter, state, action, request_id=request_id, **extra)
            except AssertionError as exc:
                if '-> 503' not in str(exc) or time.monotonic() > deadline:
                    raise
            except Exception:  # noqa: BLE001 - 연결 오류도 같은 키로 재시도
                if time.monotonic() > deadline:
                    raise
            time.sleep(1.5)


def wal_archive(cluster, api):
    """렌더링된 archive_command가 리더의 스풀에 WAL을 실패 없이 쌓는다."""
    del api
    leader = cluster.leader()
    before = int(cluster.psql_leader('SELECT archived_count FROM pg_stat_archiver;') or 0)
    cluster.psql_leader('SELECT pg_switch_wal();')
    deadline = time.monotonic()+90
    row = ''
    while time.monotonic() < deadline:
        row = cluster.psql_leader("SELECT archived_count||' '||failed_count FROM pg_stat_archiver;")
        if int(row.split()[0]) > before:
            break
        time.sleep(2)
    archived, failed = (int(part) for part in row.split())
    spooled = cluster.sh(leader, 'sudo ls /var/lib/pickup-pact/wal-spool').stdout.split()
    assert archived > before and failed == 0, row
    from demo.route.ha.backup_ops import archived_name
    assert spooled and all(archived_name(name) for name in spooled), spooled
    return dict(leader=leader, archived_count=archived, failed_count=failed, spooled_files=len(spooled))


ENTRY_NAME = 'entry.pact.internal'
ENTRY_ADDRESSES = ('10.0.0.11', '10.0.0.12')
ENTRY_HOSTS = {'10.0.0.11': 'a', '10.0.0.12': 'b'}


class EntryClient:
    """공개 주소(다중 A 레코드)를 쓰는 클라이언트. 브라우저·curl처럼 주소를 섞어 시도하고, 연결이 안 되면 다음 주소로 넘어간다."""

    def __init__(self, trust: Path, connect_timeout=3.0, *, keepalive=False, read_timeout=20.0):
        import httpx
        context = ssl.create_default_context(cafile=str(trust))
        # 기본은 요청마다 새 연결(키프얼라이브 끔): 이미 열려 있던 연결은 서버가 조용히 사라지면 읽기 시간 초과까지 멈추므로,
        # "새 요청이 남은 진입점으로 가는지"를 보려면 새 연결로 판정한다. 재사용 연결의 노출은 별도로 측정한다.
        limits = httpx.Limits(max_keepalive_connections=20 if keepalive else 0)
        self.http = httpx.Client(verify=context, trust_env=False, follow_redirects=False, limits=limits,
                                 timeout=httpx.Timeout(read_timeout, connect=connect_timeout))
        self.httpx = httpx

    def request(self, method, path, body=None, *, only=None):
        import random
        order = [only] if only else random.sample(ENTRY_ADDRESSES, k=len(ENTRY_ADDRESSES))
        last = None
        for address in order:
            try:
                response = self.http.request(method, f'https://{address}{path}', json=body, headers={'Host': ENTRY_NAME},
                                             extensions={'sni_hostname': ENTRY_NAME})
                return address, response
            except (self.httpx.ConnectError, self.httpx.ConnectTimeout) as exc:
                last = exc
        raise last


class EntryApi(base.Api):
    """주문 흐름 전체를 공개 주소(진입점)를 거쳐 보낸다."""

    def __init__(self, entry: EntryClient):
        self.entry = entry

    def call(self, letter, path, body=None):
        address, response = self.entry.request('GET' if body is None else 'POST', path, body)
        if response.status_code >= 400:
            raise AssertionError(f'{path} -> {response.status_code}: {response.text[:300]}')
        return response.json()


class EntryProbe(threading.Thread):
    """진입점 장애 중에도 공개 주소가 계속 응답하는지 0.2초마다 확인한다."""

    def __init__(self, entry: EntryClient):
        super().__init__(daemon=True)
        self.entry, self.events, self.stop_event = entry, [], threading.Event()

    def run(self):
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                address, response = self.entry.request('GET', '/ready')
                self.events.append(dict(t=started, ok=response.status_code == 200, address=address, ms=(time.monotonic()-started)*1000,
                                        status=response.status_code))
            except Exception as exc:  # noqa: BLE001
                self.events.append(dict(t=started, ok=False, address=None, ms=(time.monotonic()-started)*1000, status=type(exc).__name__))
            self.stop_event.wait(.2)

    def stop(self):
        self.stop_event.set()
        self.join(30)

    def window(self, start, end):
        return [e for e in self.events if start <= e['t'] <= end]


def _summarize(events):
    return dict(requests=len(events), failed=sum(1 for e in events if not e['ok']),
                served_by=sorted({e['address'] for e in events if e['ok']}),
                max_ms=round(max((e['ms'] for e in events), default=0)), p95_ms=round(sorted(e['ms'] for e in events)[int(len(events)*.95)-1]) if events else 0)


def ha10(cluster, api):
    """진입점 호스트 1대 중단·복귀: 공개 주소가 남은 진입점으로 계속 응답한다(서비스 중지, 방화벽 무응답 두 방식)."""
    trust = cluster.work/'entry-trust.pem'
    entry = EntryClient(trust)
    entry_api = EntryApi(entry)
    report = {}
    # 기준: 두 진입점이 각각 응답하고, 주문 전체가 공개 주소를 거쳐 끝난다.
    direct = {}
    for address in ENTRY_ADDRESSES:
        _, response = entry.request('GET', '/ready', only=address)
        direct[address] = response.status_code
    assert set(direct.values()) == {200}, direct
    baseline = entry_api.finish('a', 'b', entry_api.start('a'))
    report['baseline'] = dict(direct_ready=direct, order_through_entry=baseline)

    def outage(label, address, inject, restore, hold=14):
        letter = ENTRY_HOSTS[address]
        probe = EntryProbe(entry)
        probe.start()
        time.sleep(2)
        inject(letter)
        began = time.monotonic()  # 장애가 실제로 적용된 뒤부터 센다(적용 직전에 시작한 요청을 중단 뒤로 세지 않는다)
        try:
            time.sleep(3)  # 헬스체크·연결 실패가 반영되도록
            order = entry_api.finish('a', 'b', entry_api.start('a'))
            time.sleep(hold)
        finally:
            ended = time.monotonic()
            restore(letter)
        # 복귀: 중단했던 진입점이 다시 응답하고 두 진입점이 함께 서비스한다.
        deadline = time.monotonic()+60
        back = None
        while time.monotonic() < deadline:
            try:
                _, response = entry.request('GET', '/ready', only=address)
                if response.status_code == 200:
                    back = round(time.monotonic()-ended, 1)
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(.5)
        assert back is not None, f'{label}: 중단했던 진입점이 돌아오지 않았다'
        time.sleep(3)
        probe.stop()
        during = probe.window(began+.5, ended)
        after = probe.window(ended+back+1, time.monotonic())
        summary = _summarize(during)
        assert summary['failed'] == 0, f'{label}: 공개 주소가 {summary["failed"]}번 응답하지 못했다'
        # 복구 직전 4초(연결 시간 초과 3초 + 여유)에 시작해 연결을 기다리던 요청은 복구 뒤 SYN 재전송으로 연결될 수 있어 판정에서 뺀다.
        settled = _summarize(probe.window(began+.5, ended-4))
        assert address not in settled['served_by'], f'{label}: 중단한 진입점이 응답했다'
        summary['settled_requests'] = settled['requests']
        both = sorted({e['address'] for e in after if e['ok']})
        assert both == sorted(ENTRY_ADDRESSES), f'{label}: 복귀 뒤 두 진입점이 함께 서비스하지 않는다: {both}'
        return dict(stopped=f'pact-{letter}', during_outage=summary, order_during_outage=order, entry_back_after_s=back,
                    served_by_after_recovery=both, probe_failed_total=sum(1 for e in probe.events if not e['ok']))

    def stop_service(letter):
        cluster.sh(letter, 'sudo systemctl stop haproxy')

    def start_service(letter):
        cluster.sh(letter, 'sudo systemctl start haproxy', check=False)

    def drop_443(letter):
        cluster.sh(letter, 'sudo iptables -I INPUT 1 -p tcp --dport 443 -m comment --comment pact-entry-drop -j DROP')

    def undrop_443(letter):
        cluster.sh(letter, "sudo iptables-save | grep -v 'pact-entry-drop' | sudo iptables-restore", check=False)

    def keepalive_exposure():
        """이미 열려 있던(재사용) 연결은 진입점이 조용히 사라지면 새 연결처럼 다른 주소로 넘어가지 못한다. 그 노출 시간을 잰다."""
        warm = EntryClient(trust, keepalive=True, read_timeout=5.0)
        _, response = warm.request('GET', '/ready', only='10.0.0.12')
        assert response.status_code == 200
        drop_443('b')
        try:
            began = time.monotonic()
            try:
                warm.request('GET', '/ready', only='10.0.0.12')
                outcome = 'answered'
            except Exception as exc:  # noqa: BLE001
                outcome = type(exc).__name__
            stuck_ms = round((time.monotonic()-began)*1000)
            _, fresh = entry.request('GET', '/ready')
        finally:
            undrop_443('b')
        return dict(reused_connection_outcome=outcome, reused_connection_stuck_ms=stuck_ms, new_request_status=fresh.status_code,
                    note='재사용 연결은 클라이언트의 읽기 시간 초과까지 멈춘다. 새 요청은 남은 진입점으로 간다.')

    try:
        report['service_stop_pact_a'] = outage('service_stop', '10.0.0.11', stop_service, start_service)
        report['silent_drop_pact_b'] = outage('silent_drop', '10.0.0.12', drop_443, undrop_443)
        time.sleep(1)
        report['keepalive_exposure'] = keepalive_exposure()
    finally:
        for letter in ('a', 'b'):
            undrop_443(letter)
            start_service(letter)
    return report


def _drive_to_ready(api, host, fault='none'):
    """예약 → 매장 변경 → 제조 완료(수령 직전)까지 한 호스트로 진행한다."""
    state = api.settle(host, api.start(host, fault=fault))
    oat = next(plan for plan in state['all_plans'] if plan['store_id'] == 'oat')
    state = api.settle(host, api.command(host, state, 'transfer', quote_id=oat['quote_id']))
    minutes = max(0, state['current_plan']['start_at']-state['clock'])
    if minutes:
        state = api.command(host, state, 'advance', minutes=minutes)
    state = api.command(host, state, 'start')
    state = api.command(host, state, 'advance', minutes=max(0, state['order']['ready_at']-state['clock']))
    return api.command(host, state, 'ready')


def ha07_multihost(cluster, api):
    """청구 응답이 유실된 주문 6건을 세 호스트에서 동시에 만들고, 세 호스트의 복구 작업자가 경합해도 주문마다 청구 1건."""
    from concurrent.futures import ThreadPoolExecutor

    def pipeline(index):
        host = HOSTS[index % 3]
        state = _drive_to_ready(api, host, 'capture_reply_lost')
        state = api.command(host, state, 'claim', pickup_code=state['order']['pickup_code'])
        assert state['handoff_pending'], '청구 결과가 보류 상태로 남지 않았다'
        return state

    with ThreadPoolExecutor(6) as pool:
        pending = list(pool.map(pipeline, range(6)))
    started = time.monotonic()

    def finish(index):
        state = api.settle(HOSTS[(index+1) % 3], pending[index], timeout=180)
        return api.proof(HOSTS[(index+2) % 3], state)

    with ThreadPoolExecutor(6) as pool:
        proofs = list(pool.map(finish, range(6)))
    assert all(p['capture_count'] == 1 and p['held_krw'] == 0 for p in proofs), proofs
    return dict(orders=6, concurrent_pending=len(pending), recovered_s=round(time.monotonic()-started, 1),
                capture_counts=[p['capture_count'] for p in proofs], held_krw=[p['held_krw'] for p in proofs])


def ha08_multihost(cluster, api):
    """같은 주문의 두 번째 확정을 세 호스트에서 동시에 시도한다: 다른 키 3개 → 청구 1건, 같은 키 3개 → 같은 결과."""
    from concurrent.futures import ThreadPoolExecutor

    def attempt(args):
        host, key = args
        try:
            return 'ok', base.Api.command(api, host, state, 'claim', request_id=key, pickup_code=state['order']['pickup_code'])
        except AssertionError as exc:
            return 'rejected', str(exc)[:160]

    state = _drive_to_ready(api, 'a')
    with ThreadPoolExecutor(3) as pool:
        different = list(pool.map(attempt, [(h, uuid4().hex) for h in HOSTS]))
    final = api.settle('b', api.call('b', '/api/route/journeys/'+state['id']))
    proof = api.proof('c', final)
    assert any(kind == 'ok' for kind, _ in different), different
    # 같은 요청 키를 세 호스트에서 동시에: 모두 같은 원래 결과
    state = _drive_to_ready(api, 'b')
    key = uuid4().hex
    with ThreadPoolExecutor(3) as pool:
        same = list(pool.map(attempt, [(h, key) for h in HOSTS]))
    # 같은 키의 동시 중복: 모두 오류 없이 응답한다. 처음 요청이 처리 중이면 서비스는 두 번째 처리를 하지 않고 현재 보기(duplicate=true)를 돌려준다.
    assert all(kind == 'ok' for kind, _ in same), same
    winners = [result for _, result in same if not result.get('duplicate')]
    assert len(winners) == 1, f'원래 처리는 정확히 한 번이어야 한다: {len(winners)}'
    interim = [dict(state=result['order']['state'], handoff=(result.get('handoff') or {}).get('status')) for _, result in same if result.get('duplicate')]
    final_same = api.settle('c', api.call('c', '/api/route/journeys/'+state['id']))
    proof_same = api.proof('a', final_same)
    # 처리가 끝난 뒤 같은 키로 다시 보내면 세 호스트 모두 원래 응답과 같아야 한다.
    replays = [base.Api.command(api, host, state, 'claim', request_id=key, pickup_code=state['order']['pickup_code']) for host in HOSTS]
    assert all(r.get('duplicate') and r['order'] == winners[0]['order'] for r in replays), '완료 뒤 재전송이 원래 응답과 다르다'
    return dict(different_keys=[kind for kind, _ in different], proof_different_keys=proof,
                same_key=[kind for kind, _ in same], concurrent_duplicate_views=interim, replay_after_completion_identical=True,
                proof_same_key=proof_same)


def ha08_sequential_replay(cluster, api):
    """진단: 같은 요청 키를 순차로 다시 보냈을 때 응답이 원래 응답과 같은지(동시성 때문인지 구분한다)."""
    state = _drive_to_ready(api, 'a')
    key = uuid4().hex
    body = dict(pickup_code=state['order']['pickup_code'])
    first = base.Api.command(api, 'a', state, 'claim', request_id=key, **body)
    replays = [base.Api.command(api, host, state, 'claim', request_id=key, **body) for host in ('b', 'c', 'a')]
    fields = ('state', 'picked_up_at', 'status')
    return dict(first={k: first['order'].get(k) for k in fields}, first_duplicate=first.get('duplicate'),
                replays=[dict({k: r['order'].get(k) for k in fields}, duplicate=r.get('duplicate')) for r in replays],
                identical=all(r['order'] == first['order'] for r in replays))


def ha09_multihost(cluster, api):
    """다중 탭: 같은 주문 상태에서 세 호스트(호스트마다 2탭)가 서로 다른 키로 같은 전이를 동시에 보낸다. 전이는 한 번만 효력이 있고 끝까지 불변조건을 지킨다."""
    from concurrent.futures import ThreadPoolExecutor
    state = api.start('a')
    oat = next(plan for plan in state['all_plans'] if plan['store_id'] == 'oat')

    def tab(host):
        try:
            return 'ok', base.Api.command(api, host, state, 'transfer', request_id=uuid4().hex, quote_id=oat['quote_id'])
        except AssertionError as exc:
            return 'rejected', str(exc)[:160]

    with ThreadPoolExecutor(6) as pool:
        outcomes = list(pool.map(tab, [HOSTS[i % 3] for i in range(6)]))
    assert any(kind == 'ok' for kind, _ in outcomes), outcomes
    state = api.settle('b', api.call('b', '/api/route/journeys/'+state['id']))
    assert state['order']['price'] == 3200
    minutes = max(0, state['current_plan']['start_at']-state['clock'])
    if minutes:
        state = api.command('c', state, 'advance', minutes=minutes)
    state = api.command('a', state, 'start')
    state = api.command('b', state, 'advance', minutes=max(0, state['order']['ready_at']-state['clock']))
    state = api.command('c', state, 'ready')
    state = api.settle('a', api.command('b', state, 'claim', pickup_code=state['order']['pickup_code']))
    return dict(tab_outcomes=[kind for kind, _ in outcomes], proof=api.proof('c', state))


SCENARIOS = [('HA-10', ha10), ('HA-08-sequential-replay', ha08_sequential_replay), ('HA-07-multihost', ha07_multihost), ('HA-08-multihost', ha08_multihost), ('HA-09-multihost', ha09_multihost),
             ('WAL-ARCHIVE', wal_archive), ('HA-01', base.ha01), ('HA-02', base.ha02), ('HA-03', base.ha03),
             ('HA-04', base.ha04), ('HA-05', base.ha05), ('HA-06', base.ha06)]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--work', type=Path, required=True, help='키·인증서·접속 파일이 있는 비공개 디렉터리')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeat', type=int, default=1, choices=range(1, 6))
    parser.add_argument('--only', nargs='*', default=None)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    cluster = RealCluster(args.work)
    cluster.ensure_hold_dropins()
    api = RealApi(cluster)
    report = dict(scope=SCOPE, timings='시험 환경 측정값이며 운영 RTO/RPO가 아니다',
                  fault_injection={'host_failure': 'sysrq b 즉시 재부팅(전원 차단과 같은 효과)',
                                   'network_partition': 'DB 호스트 사이 트래픽만 방화벽으로 차단',
                                   'service_stop': 'systemd 유닛 중지'},
                  repetitions=[], passed=False)
    failures = 0
    try:
        for number in range(1, args.repeat+1):
            repetition = dict(repeat=number, scenarios=[])
            report['repetitions'].append(repetition)
            for name, scenario in SCENARIOS:
                if args.only and name not in args.only:
                    continue
                began = time.monotonic()
                row = dict(id=name)
                try:
                    cluster.ensure_units()
                    cluster.wait_cluster(members=3, timeout=240)
                    cluster.wait_apps(HOSTS)
                    row.update(scenario(cluster, api))
                    row['passed'] = True
                except Exception as exc:  # noqa: BLE001 - 실패도 모두 남긴다
                    failures += 1
                    row.update(passed=False, error=f'{type(exc).__name__}: {exc}'[:1500])
                    cluster.clear_faults()
                row['elapsed_s'] = round(time.monotonic()-began, 1)
                repetition['scenarios'].append(row)
                (args.output/'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
                print(json.dumps({'repeat': number, 'scenario': name, 'passed': row['passed'], 'elapsed_s': row['elapsed_s']}), flush=True)
    finally:
        cluster.clear_faults()
        cluster.remove_hold_dropins()
    report['passed'] = failures == 0
    (args.output/'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'passed': report['passed'], 'failures': failures, 'scope': SCOPE}), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
