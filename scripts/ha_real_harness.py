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
        etcd와 patroni를 mask해 디스크에 확정한 뒤 크래시시키고, start_host가 시험이 정한 시점에 되돌린다.
        """
        self.sh(letter, 'sudo systemctl mask etcd patroni >/dev/null 2>&1; sync')
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
        self.sh(letter, 'sudo systemctl unmask etcd patroni', check=False)
        for unit in ALL_UNITS:
            self.sh(letter, f'systemctl is-active --quiet {unit} || sudo systemctl start {unit}', timeout=150, check=False)

    def partition(self, letter):
        peers = ','.join(ADDRESSES[x] for x in HOSTS if x != letter)
        self.sh(letter, f'sudo iptables -I INPUT 1 -s {peers} -m comment --comment pact-partition -j DROP && '
                        f'sudo iptables -I OUTPUT 1 -d {peers} -m comment --comment pact-partition -j DROP')

    def heal(self, letter):
        """pact-partition 표시가 붙은 규칙만 제거한다(나머지 규칙은 그대로 다시 적용)."""
        self.sh(letter, "sudo iptables-save | grep -v 'pact-partition' | sudo iptables-restore", check=False)

    def ensure_units(self):
        """멈춘 유닛이 있으면 시작한다(앞선 시험의 장애가 남지 않게)."""
        for letter in HOSTS:
            try:
                for unit in ALL_UNITS:
                    self.sh(letter, f'systemctl is-active --quiet {unit} || sudo systemctl start {unit}', timeout=150, check=False)
            except Exception:  # noqa: BLE001
                pass

    def clear_faults(self):
        for letter in HOSTS:
            try:
                self.heal(letter)
            except Exception:  # noqa: BLE001
                pass

    def psql_leader(self, sql: str) -> str:
        leader = self.leader()
        done = self.sh(leader, f'sudo -u postgres psql -X -q -At -v ON_ERROR_STOP=1 -c {shlex.quote(sql)}', timeout=60)
        return done.stdout.strip()


class RealApi(base.Api):
    """실서버 장애 조치(약 45초 이상) 동안 주문 복구를 기다릴 수 있도록 대기 시간을 늘린다."""

    def settle(self, letter, state, timeout=60):
        return super().settle(letter, state, timeout=max(timeout, 180))


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


SCENARIOS = [('WAL-ARCHIVE', wal_archive), ('HA-01', base.ha01), ('HA-02', base.ha02), ('HA-03', base.ha03),
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
    report['passed'] = failures == 0
    (args.output/'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'passed': report['passed'], 'failures': failures, 'scope': SCOPE}), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
