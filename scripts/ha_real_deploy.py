"""승인된 독립 호스트(Oracle 시험 서버)에 ha_postgres_v1 클러스터를 배포한다.

러너가 임시 CA·비밀번호·토큰을 만들어 SSH 표준 입력으로만 올린다. 명령행 인자·로그·증거에는 비밀값이 없다.
CA 개인키는 실행이 끝나면 러너와 함께 사라지고 서버에는 올리지 않는다.
이 모듈은 한 번만 실행하는 최초 배포다. 이미 배포된 서버는 거부한다.
"""
from __future__ import annotations
import io
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'scripts'))

from demo.route.ha import inventory as inv  # noqa: E402
from demo.route.ha.scram import verifier  # noqa: E402
from ha_rehearsal_pki import PrivateAuthority  # noqa: E402

ROLES = ('order', 'merchant', 'payment')
SERVICES = ('pickup-merchant', 'pickup-order-notification', 'pickup-payment', 'pickup-order-app', 'pickup-order-worker')
SECRET_KEYS = {'order': ['ROUTE_MERCHANT_TOKEN', 'ROUTE_PAYMENT_TOKEN', 'ROUTE_PAYMENT_NOTIFY_SECRET'],
               'merchant': ['ROUTE_MERCHANT_TOKEN'], 'payment': ['ROUTE_PAYMENT_TOKEN', 'ROUTE_PAYMENT_NOTIFY_SECRET']}
PYTHONPATH = '/opt/pickup-pact:/opt/pickup-pact/services/reconciler'
DROP_IN = f'[Service]\nEnvironment=PYTHONPATH={PYTHONPATH}\nEnvironment=HOME=/tmp\nEnvironment=PYTHONDONTWRITEBYTECODE=1\nEnvironment=REPAIR_REVIEW_DB=/tmp/legacy-repair.sqlite\n'
TLS = '/etc/pickup-pact/tls'


def _add(archive: tarfile.TarFile, name: str, data: bytes | str, mode: int) -> None:
    raw = data.encode() if isinstance(data, str) else data
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = len(raw), mode, int(time.time())
    archive.addfile(info, io.BytesIO(raw))


class Deployment:
    def __init__(self, hosts, inventory_path: Path, out: Path):
        self.hosts = {h.name: h for h in hosts}
        self.db = [h for h in hosts if h.name != 'pact-backup']
        self.out = out
        self.data = inv.load(str(inventory_path))
        problems = inv.validate(self.data, 'deploy')
        if problems:
            raise SystemExit('인벤토리 deploy 단계 거부: '+'; '.join(problems))
        self.private = Path(tempfile.mkdtemp(prefix='pact-deploy-'))
        os.chmod(self.private, 0o700)
        self.passwords = {n: secrets.token_hex(24) for n in
                          ['superuser', 'replication', 'rewind', *[f'{r}_owner' for r in ROLES], *[f'{r}_runtime' for r in ROLES]]}
        self.tokens = {k: secrets.token_hex(32) for k in
                       ['ROUTE_MERCHANT_TOKEN', 'ROUTE_PAYMENT_TOKEN', 'ROUTE_PAYMENT_NOTIFY_SECRET']}
        self.evidence: dict = {'steps': []}

    def step(self, name: str, **facts) -> None:
        self.evidence['steps'].append(dict(step=name, at=time.strftime('%H:%M:%S', time.gmtime()), **facts))
        print(f'[deploy] {name}', flush=True)

    # --------------------------------------------------------------- 파일 만들기
    def build(self) -> None:
        config = self.private/'config'
        inv.render(self.data, config)
        ca = PrivateAuthority(self.private/'pki', days=5)
        etcd_client = ca.issue('etcd-client', role=None, dns=('etcd-client.pact.internal',), ips=())
        self.bundles: dict[str, bytes] = {}
        for item in inv.hosts_of(self.data):
            files: dict[str, tuple[bytes, int]] = {}
            server = dict(dns=(item.dns,), ips=(item.address,))
            certs = {'postgres': ca.issue(f'{item.name}-postgres', role=None, **server),
                     'patroni': ca.issue(f'{item.name}-patroni', role=None, **server),
                     'etcd': ca.issue(f'{item.name}-etcd', role=None, **server), 'etcd-client': etcd_client}
            for role in ROLES:
                certs[role] = ca.issue(f'{item.name}-{role}', role=role, **server)
            files['etc/pickup-pact/tls/ca.crt'] = (Path(ca.cert).read_bytes(), 0o644)
            for target, (cert, key) in certs.items():
                files[f'etc/pickup-pact/tls/{target}.crt'] = (Path(cert).read_bytes(), 0o644)
                files[f'etc/pickup-pact/tls/{target}.key'] = (Path(key).read_bytes(), 0o600)
            secret_dir = 'etc/pickup-pact/secrets/'
            patroni_env = {'PATRONI_SUPERUSER_PASSWORD': self.passwords['superuser'],
                           'PATRONI_REPLICATION_PASSWORD': self.passwords['replication'],
                           'PATRONI_REWIND_PASSWORD': self.passwords['rewind'],
                           'PICKUP_WAL_SPOOL': '/var/lib/pickup-pact/wal-spool'}
            files[secret_dir+'patroni.env'] = (''.join(f'{k}={v}\n' for k, v in patroni_env.items()).encode(), 0o600)
            for role in ROLES:
                files[secret_dir+f'{role}.env'] = (''.join(f'{k}={self.tokens[k]}\n' for k in SECRET_KEYS[role]).encode(), 0o600)
                files[secret_dir+f'{role}.pgpass'] = (
                    f'*:*:*:{inv.runtime_user(role)}:{self.passwords[role+"_runtime"]}\n'.encode(), 0o600)
            base = config/'hosts'/item.name
            for rendered in sorted(base.iterdir()):
                if rendered.name == 'haproxy.cfg':
                    continue
                files[f'etc/pickup-pact/{rendered.name}'] = (rendered.read_bytes(), 0o644)
            for unit in sorted((ROOT/'infra/ha/systemd').glob('*.service')):
                files[f'etc/systemd/system/{unit.name}'] = (unit.read_bytes(), 0o644)
                if unit.name.startswith('pickup-'):
                    files[f'etc/systemd/system/{unit.name}.d/runtime.conf'] = (DROP_IN.encode(), 0o644)
            for script in ('pickup-wal-archive', 'pickup-restore-bootstrap'):
                files[f'usr/local/bin/{script}'] = ((ROOT/'infra/ha'/script).read_bytes(), 0o755)
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode='w') as archive:
                for name, (raw, mode) in sorted(files.items()):
                    _add(archive, name, raw, mode)
            self.bundles[item.name] = buffer.getvalue()
        self.code = subprocess.run(['git', 'archive', '--format=tar', 'HEAD'], cwd=ROOT, check=True, capture_output=True).stdout
        self.config = config
        self.step('files_built', hosts=sorted(self.bundles), code_bytes=len(self.code))

    # ----------------------------------------------------------------- 원격 작업
    def pmap(self, fn, hosts=None):
        from concurrent.futures import ThreadPoolExecutor
        hosts = hosts or self.db
        with ThreadPoolExecutor(max_workers=len(hosts)) as pool:
            return dict(zip([h.name for h in hosts], pool.map(fn, hosts)))

    def upload(self, host) -> str:
        # 데이터베이스가 한 번이라도 초기화된 서버는 건드리지 않는다. etcd만 시작하다 멈춘 부분 배포는 정리 후 다시 시작한다.
        if host.run('test -d /var/lib/postgresql/17/pickup && echo data || echo nodata').stdout.strip() == 'data':
            raise SystemExit(f'{host.name}: PostgreSQL 데이터가 있는 서버다(최초 배포만 지원)')
        leftovers = host.run('test -e /etc/pickup-pact/patroni.yml -o -d /var/lib/etcd/pickup-pact && echo partial || echo fresh').stdout.strip()
        if leftovers == 'partial':
            host.run('sudo systemctl disable --now etcd patroni >/dev/null 2>&1; sudo rm -rf /var/lib/etcd/pickup-pact '
                     '/etc/pickup-pact/tls /etc/pickup-pact/secrets /etc/pickup-pact/*.yml /etc/pickup-pact/*.env', timeout=120)
            host.run('sudo install -d -m 0755 /etc/pickup-pact && sudo install -d -m 0750 /etc/pickup-pact/tls /etc/pickup-pact/secrets')
            self.evidence.setdefault('cleaned_partial', []).append(host.name)
        host.run_bytes('sudo tar -xp -C /', self.bundles[host.name])
        host.run('sudo mkdir -p /opt/pickup-pact')
        host.run_bytes('sudo tar -x -C /opt/pickup-pact', self.code)
        done = host.run_script(ROOT/'infra/ha/hosts/finalize.sh', ['db'], timeout=900)
        return done.stdout.strip()

    def etcd_health(self, timeout=120) -> list:
        endpoints = ','.join(f'https://{h.spec["private"]}:2379' for h in self.db)
        command = (f'sudo /usr/local/bin/etcdctl --endpoints={endpoints} --cacert={TLS}/ca.crt --cert={TLS}/etcd-client.crt '
                   f'--key={TLS}/etcd-client.key endpoint health --cluster -w json')
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            done = self.db[0].run(command, check=False)
            if done.returncode == 0:
                health = json.loads(done.stdout)
                if len(health) == 3 and all(item.get('health') for item in health):
                    return [dict(endpoint=item['endpoint'], health=True) for item in health]
            time.sleep(2)
        raise AssertionError('etcd 정족수가 건강해지지 않았다')

    def members(self, via=None) -> list[dict]:
        for host in ([via] if via else self.db):
            done = host.run('sudo -u postgres /opt/patroni/bin/patronictl -c /etc/pickup-pact/patroni.yml list -f json', check=False, timeout=60)
            if done.returncode == 0 and done.stdout.strip():
                return json.loads(done.stdout)
        raise RuntimeError('Patroni 구성원 목록을 읽을 수 없다')

    def wait_cluster(self, timeout=300) -> list[dict]:
        deadline = time.monotonic()+timeout
        last = None
        while time.monotonic() < deadline:
            try:
                last = self.members()
                leaders = [m for m in last if m.get('Role') == 'Leader' and m.get('State') == 'running']
                sync = [m for m in last if m.get('Role') == 'Sync Standby' and m.get('State') == 'streaming']
                streaming = [m for m in last if m.get('Role') in {'Sync Standby', 'Replica'} and m.get('State') == 'streaming']
                if len(leaders) == 1 and sync and len(streaming) >= 2:
                    return last
            except (RuntimeError, ValueError):
                pass
            time.sleep(3)
        raise AssertionError('Patroni 클러스터가 수렴하지 않았다: '+json.dumps(last))

    def psql(self, host, sql: str, database='postgres') -> None:
        done = host.run_bytes(f'sudo -u postgres psql -X -q -v ON_ERROR_STOP=1 -d {database} -f -', sql.encode(), timeout=120)
        if done.returncode:
            raise AssertionError('psql 실패: '+done.stderr[-600:])

    def owner_dsn(self, role: str) -> str:
        hosts, ports = inv.order_dsn_hosts(self.data)
        return (f'host={hosts} port={ports} dbname={inv.DATABASES[role]} user={inv.owner_user(role)} sslmode=verify-full '
                f'sslrootcert={TLS}/ca.crt passfile=/dev/shm/pact-migrate/{role}-owner.pgpass '
                f'target_session_attrs=read-write connect_timeout=3')

    def bootstrap_roles(self, leader) -> None:
        self.psql(leader, (self.config/'cluster/bootstrap.sql').read_text())
        statements = [f"ALTER ROLE {user} PASSWORD '{verifier(self.passwords[f'{role}_{kind}'])}';"
                      for role in ROLES for kind, user in (('owner', inv.owner_user(role)), ('runtime', inv.runtime_user(role)))]
        self.psql(leader, '\n'.join(statements)+'\n')
        leader.run('sudo install -d -o pickup -g pickup -m 0700 /dev/shm/pact-migrate')
        try:
            for role in ROLES:
                leader.run_bytes(f'sudo -u pickup tee /dev/shm/pact-migrate/{role}-owner.pgpass >/dev/null && '
                                 f'sudo chmod 0600 /dev/shm/pact-migrate/{role}-owner.pgpass',
                                 f'*:*:*:{inv.owner_user(role)}:{self.passwords[role+"_owner"]}\n'.encode())
                done = leader.run(
                    f'cd /opt/pickup-pact && sudo -u pickup env PYTHONPATH={PYTHONPATH} PICKUP_MIGRATE_DSN={shlex.quote(self.owner_dsn(role))} '
                    f'/opt/pickup-pact/.venv/bin/python -m demo.route.ha.migrate apply --role {role} --runtime-user {inv.runtime_user(role)}',
                    check=False, timeout=180)
                if done.returncode:
                    raise AssertionError(f'마이그레이션 실패({role}): '+done.stderr[-600:])
                self.step('migrated', role=role, result=json.loads(done.stdout))
        finally:
            leader.run('sudo rm -rf /dev/shm/pact-migrate', check=False)

    def app_probe(self, host) -> dict:
        cert = f'--cacert {TLS}/ca.crt --cert {TLS}/order.crt --key {TLS}/order.key'
        base = f'https://{host.spec["private"]}:8000'
        ready = host.run(f'sudo curl -s -o /dev/null -w "%{{http_code}}" {cert} {base}/ready', check=False).stdout.strip()
        runtime = host.run(f'sudo curl -s {cert} {base}/api/route/runtime', check=False).stdout
        try:
            return dict(ready=ready, runtime=json.loads(runtime))
        except ValueError:
            return dict(ready=ready, runtime=None)

    def wait_apps(self, timeout=180) -> dict:
        deadline = time.monotonic()+timeout
        last: dict = {}
        pending = {h.name for h in self.db}
        while pending and time.monotonic() < deadline:
            for host in [h for h in self.db if h.name in pending]:
                last[host.name] = self.app_probe(host)
                runtime = last[host.name].get('runtime') or {}
                if last[host.name]['ready'] == '200' and runtime.get('storage_backend') == 'postgresql' and runtime.get('automatic_recovery'):
                    pending.discard(host.name)
            time.sleep(2)
        if pending:
            raise AssertionError('앱이 준비되지 않았다: '+json.dumps({k: v.get('ready') for k, v in last.items()}))
        return last

    # ---------------------------------------------------------------- 전체 순서
    def run(self) -> dict:
        began = time.monotonic()
        try:
            self.build()
            self.step('uploaded', hosts=self.pmap(self.upload))
            self.pmap(lambda h: h.run('sudo systemctl enable --now etcd', timeout=240))
            self.step('etcd_started', health=self.etcd_health())
            for host in self.db:
                host.run('sudo systemctl enable --now patroni', timeout=120)
            members = self.wait_cluster()
            self.step('patroni_converged', members=[(m['Member'], m['Role'], m['State'], m.get('TL')) for m in members])
            leader = self.hosts[next(m['Member'] for m in members if m['Role'] == 'Leader')]
            self.bootstrap_roles(leader)
            for host in self.db:
                for unit in SERVICES:
                    host.run(f'sudo systemctl enable --now {unit}', timeout=120)
            apps = self.wait_apps()
            self.step('apps_ready', apps={k: dict(ready=v['ready'], runtime=v['runtime']) for k, v in apps.items()})
            states = self.pmap(lambda h: h.run('systemctl is-active etcd patroni '+' '.join(SERVICES), check=False).stdout.split())
            self.step('units_active', states=states)
            self.evidence.update(passed=all(s == 'active' for row in states.values() for s in row),
                                 seconds=round(time.monotonic()-began, 1),
                                 inventory_sha256=inv.digest(self.data),
                                 scope=(self.data.get('approval', {}).get('accepted_exceptions') or [{}])[0].get('scope'))
        except BaseException as exc:  # noqa: BLE001 - 실패도 증거로 남긴다
            self.evidence.update(passed=False, error=f'{type(exc).__name__}: {str(exc)[:1500]}')
            raise
        finally:
            self.out.mkdir(parents=True, exist_ok=True)
            (self.out/'deploy.json').write_text(json.dumps(self.evidence, ensure_ascii=False, indent=2, default=str))
            shutil.rmtree(self.private, ignore_errors=True)
        return self.evidence
