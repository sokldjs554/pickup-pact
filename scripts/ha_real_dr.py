#!/usr/bin/env python3
"""독립 호스트 DR 시험 도구(pact-backup에서 실행). 승인된 시험 서버 전용.

backup 단계(클러스터는 건드리지 않는다):
  1. 이 호스트에 자체 서명 인증서의 HTTPS append-only Restic 저장소를 새로 만든다(`pickup-backup-store` 임시 유닛).
  2. 실제 클러스터에 주문 둘을 만든다(하나는 완료, 하나는 청구 응답 유실로 진행 중).
  3. 리더에서 pg_basebackup(복제 사용자, TLS)으로 기본 백업을 받고 복원 지점을 만든 뒤 필요한 WAL을 리더 스풀에서 가져온다.
  4. 봉인(sha256)한 묶음을 저장소에 올리고, 영수증을 받기 전에 전체 읽기 검사와 되읽기 검증을 한다.
  5. 음성 대조: 잘못된 암호화 키·서버 자격·신뢰되지 않는 인증서, 스냅샷 삭제(403), 저장소 중단·용량 초과, 암호문 변조.
저장소 암호화 키·서버 자격은 /dev/shm의 비공개 디렉터리에만 두고 끝나면 지운다(인벤토리의 보관 위치 선언과 같다).
증거에는 비밀값·암호문이 없다. 시간은 시험 환경의 값이며 운영 RTO/RPO가 아니다.
"""
from __future__ import annotations
import argparse
import base64
from dataclasses import replace
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ha_real_harness import ADDRESSES, HOSTS, RealApi, RealCluster  # noqa: E402
from demo.route.ha import inventory as inv  # noqa: E402
from demo.route.ha.backup import required_wal, seal_bundle, verify_bundle  # noqa: E402
from demo.route.ha.scram import verifier  # noqa: E402
from demo.route.ha.remote_backup import ResticArchive, ResticSettings  # noqa: E402

SCOPE = 'oracle_single_ad_fault_domains'
STORE_DNS, STORE_ADDRESS, STORE_PORT = 'backup.pact.internal', '10.0.0.20', 8000
STORE_UNIT = 'pickup-backup-store'
STORE_DATA = '/var/lib/restic-repo'
STORE_CONF = '/etc/pickup-pact/backup-store'


class RealDR(RealCluster):
    def __init__(self, workdir: Path, staging: Path):
        super().__init__(workdir)
        self.secrets = self.work/'dr'
        self.stage = Path(staging)
        self.checks: list[str] = []
        self.encryption = secrets.token_hex(32)
        self.writer = secrets.token_hex(24)
        self.settings: ResticSettings | None = None
        self.archive: ResticArchive | None = None

    # ------------------------------------------------------------- 저장소
    def local(self, command: str, *, timeout=120, check=True) -> subprocess.CompletedProcess:
        done = subprocess.run(['bash', '-c', command], capture_output=True, text=True, timeout=timeout)
        if check and done.returncode:
            raise RuntimeError(f'local command failed ({done.returncode}): {done.stderr.strip()[-300:]}')
        return done

    def setup_store(self, quota_bytes=8*1024**3):
        self.secrets.mkdir(mode=0o700, exist_ok=True)
        for name, value in [('password_file', self.encryption), ('username_file', 'backup'), ('credential_file', self.writer)]:
            path = self.secrets/name
            path.write_text(value+'\n')
            path.chmod(0o600)
        self.cert, self.store_key = self.secrets/'store-ca.pem', self.secrets/'store.key'
        self._certificate(self.cert, self.store_key)
        subprocess.run(['htpasswd', '-iBc', str(self.secrets/'htpasswd'), 'backup'], input=self.writer+'\n',
                       capture_output=True, text=True, check=True, timeout=15)
        # 저장소 프로세스는 별도 사용자(restic)로 실행하고, 설정 파일은 그 사용자만 읽는다.
        self.local(f'sudo rm -rf {STORE_CONF} && sudo install -d -m 0750 -o root -g restic {STORE_CONF} && '
                   f'sudo install -m 0640 -o root -g restic {self.cert} {STORE_CONF}/tls.crt && '
                   f'sudo install -m 0640 -o root -g restic {self.store_key} {STORE_CONF}/tls.key && '
                   f'sudo install -m 0640 -o root -g restic {self.secrets}/htpasswd {STORE_CONF}/htpasswd && '
                   f'sudo rm -rf {STORE_DATA}/backup && sudo install -d -m 0700 -o restic -g restic {STORE_DATA}')
        self.start_store(quota_bytes)
        self.settings = ResticSettings(repository=f'rest:https://{STORE_DNS}:{STORE_PORT}/backup/',
                                       allowed_authority=f'{STORE_DNS}:{STORE_PORT}', ca_file=self.cert,
                                       password_file=self.secrets/'password_file', username_file=self.secrets/'username_file',
                                       credential_file=self.secrets/'credential_file')
        self.archive = ResticArchive(self.settings)
        self.archive._run(['init', '--repository-version', '2'])
        self.local(f'sudo test -f {STORE_DATA}/backup/config')

    @staticmethod
    def _certificate(cert: Path, key: Path):
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '3', '-subj', '/CN='+STORE_DNS,
                        '-addext', f'subjectAltName=DNS:{STORE_DNS},IP:{STORE_ADDRESS}', '-keyout', str(key), '-out', str(cert)],
                       capture_output=True, check=True, timeout=30)
        key.chmod(0o600)

    def start_store(self, quota_bytes=8*1024**3):
        self.stop_store()
        self.local(f'sudo systemd-run --quiet --unit={STORE_UNIT} --collect --property=User=restic --property=Restart=no '
                   f'/usr/local/bin/rest-server --listen {STORE_ADDRESS}:{STORE_PORT} --path {STORE_DATA} '
                   f'--htpasswd-file {STORE_CONF}/htpasswd --append-only --private-repos --tls --tls-cert {STORE_CONF}/tls.crt '
                   f'--tls-key {STORE_CONF}/tls.key --tls-min-ver 1.3 --max-size {quota_bytes}')
        deadline = time.monotonic()+20
        last = ''
        while time.monotonic() < deadline:
            try:
                with self._opener().open(self._request('config'), timeout=2):
                    return
            except HTTPError as exc:
                if exc.code == 404:
                    return  # 인증된 빈 저장소(아직 초기화 전)
                raise OSError(f'저장소 인증이 시작 중에 실패했다({exc.code})') from None
            except (URLError, OSError) as exc:
                last = f'{type(exc).__name__}: {exc}'[:200]
                time.sleep(.3)
        logs = self.local(f'sudo journalctl -u {STORE_UNIT} --no-pager -n 12 -o cat 2>&1 | cut -c1-200; '
                          f'systemctl is-active {STORE_UNIT}; ls -ld {STORE_DATA} {STORE_CONF}; sudo ss -ltn | grep {STORE_PORT}', check=False).stdout
        raise OSError('HTTPS 저장소가 준비되지 않았다(마지막 오류 '+last+'): '+logs.replace('\n', ' | ')[:900])

    def stop_store(self):
        self.local(f'sudo systemctl stop {STORE_UNIT} 2>/dev/null; sudo systemctl reset-failed {STORE_UNIT} 2>/dev/null; true', check=False)

    def _opener(self):
        return build_opener(ProxyHandler({}), HTTPSHandler(context=ssl.create_default_context(cafile=str(self.cert))))

    def _request(self, suffix, method='GET'):
        auth = base64.b64encode(('backup:'+self.writer).encode()).decode()
        return Request(f'https://{STORE_DNS}:{STORE_PORT}/backup/'+suffix, method=method, headers={'Authorization': 'Basic '+auth})

    def teardown_store(self):
        self.stop_store()
        self.local(f'sudo rm -rf {STORE_DATA}/backup {STORE_CONF}', check=False)

    # --------------------------------------------------------- 주문과 기본 백업
    def seed_orders(self, api):
        from verify_ha_cluster import identity  # noqa: F401  (import 검사용)
        import verify_ha_cluster as base
        first = api.start('a')
        completed = api.finish('a', 'b', first)
        pending = api.settle('a', api.start('a', fault='capture_reply_lost'))
        oat = next(plan for plan in pending['all_plans'] if plan['store_id'] == 'oat')
        pending = api.settle('b', api.command('b', pending, 'transfer', quote_id=oat['quote_id']))
        del base
        return completed, pending

    def base_backup(self) -> dict:
        leader = self.leader()
        replication = self.sh(leader, "sudo sed -n 's/^PATRONI_REPLICATION_PASSWORD=//p' /etc/pickup-pact/secrets/patroni.env").stdout.strip()
        base_dir = self.stage/'base'
        started = time.monotonic()
        env = {**os.environ, 'PGPASSWORD': replication, 'PGSSLMODE': 'verify-full', 'PGSSLROOTCERT': str(self.ca)}
        for tool in (['pg_basebackup', '-h', f'pact-{leader}.pact.internal', '-U', 'replicator', '-D', str(base_dir), '-Fp', '-Xstream',
                      '--checkpoint=fast', '--manifest-checksums=SHA256'], ['pg_verifybackup', str(base_dir)]):
            done = subprocess.run(tool, env=env, capture_output=True, text=True, timeout=600)
            if done.returncode:
                raise AssertionError(f'{tool[0]} 실패: '+done.stderr[-300:].replace(replication, '[redacted]'))
        manifest = json.loads((base_dir/'backup_manifest').read_text())
        return dict(leader=leader, start_lsn=manifest['WAL-Ranges'][0]['Start-LSN'], timeline=manifest['WAL-Ranges'][0]['Timeline'],
                    seconds=round(time.monotonic()-started, 2))

    def leader_sql(self, statement: str) -> str:
        return self.psql_leader(statement)

    def restore_point(self, target: str) -> dict:
        self.leader_sql(f"SELECT pg_create_restore_point('{target}');")
        return dict(target=target, lsn=self.leader_sql('SELECT pg_current_wal_flush_lsn();'), leader=self.leader())

    def collect_bundle(self, destination: Path, *, base: dict, target: dict):
        self.leader_sql('SELECT pg_switch_wal();')
        cluster_id = self.leader_sql('SELECT system_identifier FROM pg_control_system();')
        segment = int(self.leader_sql("SELECT pg_size_bytes(current_setting('wal_segment_size'));"))
        needed = required_wal(base['start_lsn'], target['lsn'], timeline=base['timeline'], segment_bytes=segment)
        leader = target['leader']
        spool = '/var/lib/pickup-pact/wal-spool/'
        deadline = time.monotonic()+90
        while time.monotonic() < deadline:
            listed = set(self.sh(leader, f'sudo ls {spool}').stdout.split())
            if set(needed) <= listed:
                break
            time.sleep(1)
        else:
            raise AssertionError('필요한 WAL이 스풀에 쌓이지 않았다')
        destination.mkdir(parents=True)
        (destination/'wal').mkdir()
        shutil.move(str(self.stage/'base'), str(destination/'base'))
        for name in needed:
            data = subprocess.run(self._ssh(leader)+['sudo', 'cat', spool+name], capture_output=True, timeout=120).stdout
            (destination/'wal'/name).write_bytes(data)
        commit = (Path(__file__).resolve().parents[1]/'.pact-commit').read_text().strip()
        metadata = dict(cluster_id=cluster_id, source_commit=commit, timeline=base['timeline'],
                        wal_segment_size=segment, start_lsn=base['start_lsn'], target_lsn=target['lsn'],
                        scope='oracle_single_ad_fault_domains')
        return seal_bundle(destination, metadata), dict(metadata, required_wal=needed)

    # -------------------------------------------------------------- 음성 대조
    def negative_controls(self, receipt, bundle: Path, digest: str, cluster_id: str) -> list[str]:
        def rejects(label, operation):
            try:
                operation()
            except (OSError, ValueError):
                self.checks.append(label)
            else:
                raise AssertionError(label+' 거부되지 않았다')
        bad = self.secrets/'wrong'
        bad.write_text(secrets.token_hex(32)+'\n')
        bad.chmod(0o600)
        rejects('wrong_encryption_key_rejected', lambda: ResticArchive(replace(self.settings, password_file=bad), command_timeout=15).repository_id())
        rejects('wrong_server_credential_rejected', lambda: ResticArchive(replace(self.settings, credential_file=bad), command_timeout=15).repository_id())
        other, other_key = self.secrets/'other-ca.pem', self.secrets/'other.key'
        self._certificate(other, other_key)
        rejects('untrusted_tls_certificate_rejected', lambda: ResticArchive(replace(self.settings, ca_file=other), command_timeout=15).repository_id())
        try:
            with self._opener().open(self._request('snapshots/'+receipt['snapshot_id'], 'DELETE'), timeout=10):
                pass
        except HTTPError as exc:
            if exc.code != 403:
                raise AssertionError(f'append-only 확인이 예상 밖 상태를 돌려줬다: {exc.code}') from None
            self.checks.append('server_refused_snapshot_deletion_403')
        else:
            raise AssertionError('쓰기 자격으로 고정된 스냅샷을 지울 수 있다')
        self.stop_store()
        rejects('unavailable_store_did_not_issue_receipt', lambda: ResticArchive(self.settings, command_timeout=20).upload(
            bundle, expected_sha256=digest, expected_cluster_id=cluster_id))
        self.start_store(quota_bytes=1)
        rejects('quota_failure_did_not_issue_receipt', lambda: ResticArchive(self.settings, command_timeout=60).upload(
            bundle, expected_sha256=digest, expected_cluster_id=cluster_id))
        self.start_store()
        pack = self.local(f"sudo find {STORE_DATA}/backup/data -type f | head -1").stdout.strip()
        original = subprocess.run(['sudo', 'cat', pack], capture_output=True, timeout=60).stdout
        flipped = bytes([original[0] ^ 1])+original[1:]
        try:
            subprocess.run(['sudo', 'tee', pack], input=flipped, capture_output=True, timeout=60, check=True)
            rejects('ciphertext_corruption_rejected', lambda: ResticArchive(self.settings, command_timeout=120)._run(['check', '--read-data']))
        finally:
            subprocess.run(['sudo', 'tee', pack], input=original, capture_output=True, timeout=60, check=True)
        self.archive._run(['check', '--read-data'])
        return list(self.checks)


def backup_stage(args) -> dict:
    stage = Path(args.stage)
    if stage.exists():
        subprocess.run(['rm', '-rf', str(stage)], check=True)
    stage.mkdir(mode=0o700)
    dr = RealDR(args.work, stage)
    dr.ensure_hold_dropins()
    api = RealApi(dr)
    report: dict = dict(scope=SCOPE, steps=[], timings='시험 환경 측정값이며 운영 RTO/RPO가 아니다')
    try:
        began = time.monotonic()
        dr.ensure_units()
        dr.wait_cluster(members=3, timeout=240)
        dr.wait_apps(HOSTS)
        dr.setup_store()
        report['store_ready_s'] = round(time.monotonic()-began, 1)
        completed, pending = dr.seed_orders(api)
        report['steps'].append('주문 둘 생성(완료 1, 청구 응답 유실로 진행 중 1)')
        base = dr.base_backup()
        report['base_backup'] = base
        target = dr.restore_point('pact_target_'+secrets.token_hex(6))
        report['restore_point'] = dict(name=target['target'], lsn=target['lsn'], leader=target['leader'])
        bundle = stage/'bundle'
        seal, metadata = dr.collect_bundle(bundle, base=base, target=target)
        report['bundle'] = dict(required_wal=len(metadata['required_wal']), timeline=metadata['timeline'], seal_sha256=seal)
        started = time.monotonic()
        receipt = dr.archive.upload(bundle, expected_sha256=seal, expected_cluster_id=metadata['cluster_id'])
        report['upload_and_readback_s'] = round(time.monotonic()-started, 1)
        report['receipt'] = dict(snapshot_id=receipt['snapshot_id'], repository_id=receipt['repository_id'], target_lsn=receipt['target_lsn'])
        report['negative_controls'] = dr.negative_controls(receipt, bundle, seal, metadata['cluster_id'])
        report['passed'] = True
    except Exception as exc:  # noqa: BLE001 - 실패도 증거로 남긴다
        report.update(passed=False, error=f'{type(exc).__name__}: {exc}'[:1500])
    finally:
        try:
            dr.teardown_store()
        finally:
            subprocess.run(['rm', '-rf', str(stage), str(dr.secrets)], check=False)
            dr.clear_faults()
            dr.remove_hold_dropins()
    return report


# =============================================================== 복원 단계(전체)
ROLES = ('order', 'merchant', 'payment')
APPROVED_PATHS = ('/var/lib/postgresql/17/pickup', '/var/lib/pickup-pact/wal-spool', '/var/lib/etcd/pickup-pact',
                  '/var/lib/pickup-pact/restore')
SERVICE_UNITS = ['pickup-order-worker', 'pickup-order-app', 'pickup-order-notification', 'pickup-payment', 'pickup-merchant']
START_ORDER = ['pickup-merchant', 'pickup-order-notification', 'pickup-payment', 'pickup-order-app', 'pickup-order-worker']
INVENTORY = Path(__file__).resolve().parents[1]/'infra/ha/inventory.oracle-osaka.yaml'
PRODUCTION_ADDRESSES = ('pickup-pact-demo.onrender.com',)


def _put(dr: 'RealDR', letter: str, path: str, data: bytes, *, mode='0644', owner='root:root') -> None:
    user, group = owner.split(':')
    done = subprocess.run(dr._ssh(letter)+['sudo', 'install', '-m', mode, '-o', user, '-g', group, '/dev/stdin', path],
                          input=data, capture_output=True, timeout=120)
    if done.returncode:
        raise RuntimeError(f'pact-{letter}: 파일을 쓰지 못했다({done.returncode})')


class Guard:
    """파괴 명령은 승인된 시험 호스트와 고정된 경로만 받는다. 운영 주소·임의 DSN·다른 경로는 거부한다."""

    def __init__(self, data: dict):
        self.hosts = {h.name[-1] for h in inv.hosts_of(data) if h.name in set(data['approval']['destructive']['hosts'])}
        self.scope = data['name']
        self.production = {str(v).lower() for v in data['approval']['production_addresses']}

    def check(self, target: str, path: str) -> None:
        if str(target).lower() in self.production or '.' in str(target) or '@' in str(target) or '=' in str(target):
            raise PermissionError('운영 주소나 임의 접속 문자열은 파괴 대상이 될 수 없다')
        if target not in self.hosts:
            raise PermissionError('승인된 시험 호스트가 아니다')
        if path not in APPROVED_PATHS:
            raise PermissionError('승인된 시험 경로가 아니다')


def _passwords() -> dict:
    return {f'{role}_{kind}': secrets.token_hex(24) for role in ROLES for kind in ('owner', 'runtime')}


def _tokens() -> dict:
    return {k: secrets.token_hex(32) for k in ('ROUTE_MERCHANT_TOKEN', 'ROUTE_PAYMENT_TOKEN', 'ROUTE_PAYMENT_NOTIFY_SECRET')}


SECRET_KEYS = {'order': ['ROUTE_MERCHANT_TOKEN', 'ROUTE_PAYMENT_TOKEN', 'ROUTE_PAYMENT_NOTIFY_SECRET'],
               'merchant': ['ROUTE_MERCHANT_TOKEN'], 'payment': ['ROUTE_PAYMENT_TOKEN', 'ROUTE_PAYMENT_NOTIFY_SECRET']}


class FullDR(RealDR):
    def __init__(self, workdir, staging):
        super().__init__(workdir, staging)
        self.data = inv.load(str(INVENTORY))
        self.guard = Guard(self.data)
        self.restore_id = secrets.token_hex(4)
        self.passwords = _passwords()
        self.tokens = _tokens()
        self.leader_letter = 'a'

    # ---- 진행 중 주문과 복원 지점
    def pending_capture(self, api, state):
        from verify_ha_cluster_restore import progress_to_pickup
        for letter in HOSTS:
            self.stop_part(letter, 'worker')
        state = progress_to_pickup(api, 'c', state)
        state = api.command('c', state, 'claim', pickup_code=state['order']['pickup_code'])
        assert state['handoff_pending'], '청구 결과가 보류 상태로 남지 않았다'
        return state

    # ---- 파괴
    def destroy(self) -> dict:
        removed = {}
        for letter in HOSTS:
            self.guard.check(letter, '/var/lib/postgresql/17/pickup')
            done = self.sh(letter, f"test -f /etc/pickup-pact/patroni.yml && grep -qx 'scope: {self.guard.scope}' /etc/pickup-pact/patroni.yml && echo ours", check=False)
            if done.stdout.strip() != 'ours':
                raise PermissionError(f'pact-{letter}: 이 시험이 배포한 클러스터가 아니다. 삭제하지 않는다')
        for letter in HOSTS:
            self.sh(letter, 'sudo systemctl stop '+' '.join(SERVICE_UNITS)+' patroni etcd', timeout=240, check=False)
            self.sh(letter, 'sudo pkill -KILL -u postgres -x postgres || true', check=False)
            for path in APPROVED_PATHS:
                self.guard.check(letter, path)
            glob = '/var/lib/pickup-pact/wal-spool/*'
            self.sh(letter, 'sudo rm -rf /var/lib/postgresql/17/pickup /var/lib/etcd/pickup-pact /var/lib/pickup-pact/restore '
                            f'{glob} && sync')
            left = self.sh(letter, 'ls /var/lib/postgresql/17/ /var/lib/etcd/ /var/lib/pickup-pact/wal-spool/ 2>/dev/null | tr "\\n" " "').stdout
            removed[f'pact-{letter}'] = left.strip()
            assert 'pickup' not in left.replace('pickup-pact', '').split(), left
        return removed

    # ---- 복원
    def restore(self, receipt, seal, cluster_id, target_name) -> dict:
        report: dict = {}
        fetched = self.stage/'fetched'
        started = time.monotonic()
        fetched.mkdir()
        self.archive.restore(receipt, fetched/'bundle', expected_sha256=seal, expected_cluster_id=cluster_id)
        report['downloaded_s'] = round(time.monotonic()-started, 1)
        # 이전 값 기록(이전 토큰 거부 확인용), 그리고 새 비밀값으로 교체
        old = {k: v for k, v in (line.split('=', 1) for line in self.sh('a', 'sudo cat /etc/pickup-pact/secrets/order.env').stdout.split() if '=' in line)}
        self.previous_tokens = old
        # 세대 2로 렌더링한 설정(복원 지정 리더만 복원 부트스트랩)
        data = {**self.data, 'operating': {'generation': 2},
                'restore': {'leader': f'pact-{self.leader_letter}', 'base_dir': '/var/lib/pickup-pact/restore/base',
                            'wal_dir': '/var/lib/pickup-pact/restore/wal', 'target_name': target_name}}
        problems = inv.validate(data, 'review')
        if problems:
            raise AssertionError('복원 인벤토리 거부: '+'; '.join(problems))
        out = self.stage/'rendered'
        inv.render(data, out)
        for letter in HOSTS:
            base = out/'hosts'/f'pact-{letter}'
            for rendered in sorted(base.iterdir()):
                if rendered.name == 'haproxy.cfg':
                    continue
                _put(self, letter, f'/etc/pickup-pact/{rendered.name}', rendered.read_bytes())
            for role in ROLES:
                text = ''.join(f'{k}={self.tokens[k]}\n' for k in SECRET_KEYS[role])
                _put(self, letter, f'/etc/pickup-pact/secrets/{role}.env', text.encode(), mode='0600')
                _put(self, letter, f'/etc/pickup-pact/secrets/{role}.pgpass',
                     f'*:*:*:{inv.runtime_user(role)}:{self.passwords[role+"_runtime"]}\n'.encode(), mode='0600', owner='pickup:pickup')
            for script in ('pickup-wal-archive', 'pickup-restore-bootstrap'):
                _put(self, letter, f'/usr/local/bin/{script}', (Path(__file__).resolve().parents[1]/'infra/ha'/script).read_bytes(), mode='0755')
        # 내려받아 검증한 묶음을 지정 리더에만 올린다.
        lead = self.leader_letter
        self.sh(lead, 'sudo install -d -m 0700 -o postgres -g postgres /var/lib/pickup-pact/restore')
        pipe = subprocess.run(f"tar -C {fetched/'bundle'} -cf - base wal | "+' '.join(shlex.quote(a) for a in self._ssh(lead))+
                              " 'sudo tar -x -C /var/lib/pickup-pact/restore --no-same-owner && sudo chown -R postgres:postgres /var/lib/pickup-pact/restore && sudo chmod -R go-rwx /var/lib/pickup-pact/restore'",
                              shell=True, capture_output=True, timeout=600)
        if pipe.returncode:
            raise RuntimeError('복원 묶음을 지정 리더에 올리지 못했다: '+pipe.stderr.decode()[-200:])
        shutil.rmtree(fetched, ignore_errors=True)
        # 새 etcd → 지정 리더 → 나머지
        for letter in HOSTS:
            self.sh(letter, 'sudo rm -f /etc/pickup-pact/hold; true', check=False)
        procs = [subprocess.Popen(self._ssh(letter)+['sudo', 'systemctl', 'start', 'etcd'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for letter in HOSTS]
        for proc in procs:
            proc.wait(timeout=240)
        self.wait_etcd()
        self.sh(lead, 'sudo systemctl start patroni', timeout=120)
        deadline = time.monotonic()+420
        became = False
        while time.monotonic() < deadline and not became:
            try:
                became = self.leader(via=lead) == lead
            except RuntimeError:
                pass
            if not became:
                time.sleep(3)
        if not became:
            raise AssertionError('복원된 구성원이 리더가 되지 못했다')
        report['restored_leader_after_s'] = round(time.monotonic()-started, 1)
        for letter in HOSTS:
            if letter != lead:
                self.sh(letter, 'sudo systemctl start patroni', timeout=120)
        self.wait_cluster(members=3, timeout=420)
        statements = [f"ALTER ROLE {user} PASSWORD '{verifier(self.passwords[f'{role}_{kind}'])}';"
                      for role in ROLES for kind, user in (('owner', inv.owner_user(role)), ('runtime', inv.runtime_user(role)))]
        done = subprocess.run(self._ssh(lead)+['sudo', '-u', 'postgres', 'psql', '-X', '-q', '-v', 'ON_ERROR_STOP=1', '-d', 'postgres', '-f', '-'],
                              input='\n'.join(statements).encode()+b'\n', capture_output=True, timeout=120)
        if done.returncode:
            raise AssertionError('역할 비밀번호 교체 실패')
        report['bumped'] = self.bump_generation(lead, expected=1)
        for letter in HOSTS:
            for unit in START_ORDER:
                self.sh(letter, f'sudo systemctl start {unit}', timeout=120)
        self.wait_apps(HOSTS, timeout=300)
        report['apps_ready_after_loss_s'] = round(time.monotonic()-started, 1)
        report['restored_members'] = [(m['Member'], m['Role'], m['State'], m.get('TL')) for m in self.members()]
        return report

    def wait_etcd(self, timeout=120):
        endpoints = ','.join(f'https://{ADDRESSES[x]}:2379' for x in HOSTS)
        command = (f'sudo /usr/local/bin/etcdctl --endpoints={endpoints} --cacert=/etc/pickup-pact/tls/ca.crt '
                   '--cert=/etc/pickup-pact/tls/etcd-client.crt --key=/etc/pickup-pact/tls/etcd-client.key endpoint health --cluster -w json')
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            done = self.sh('a', command, check=False, timeout=40)
            if done.returncode == 0:
                health = json.loads(done.stdout)
                if len(health) == 3 and all(item.get('health') for item in health):
                    return
            time.sleep(2)
        raise AssertionError('새 etcd 정족수가 건강해지지 않았다')

    def bump_generation(self, letter, *, expected: int) -> list:
        self.sh(letter, 'sudo install -d -o pickup -g pickup -m 0700 /dev/shm/pact-migrate')
        results = []
        try:
            for role in ROLES:
                _put(self, letter, f'/dev/shm/pact-migrate/{role}-owner.pgpass',
                     f'*:*:*:{inv.owner_user(role)}:{self.passwords[role+"_owner"]}\n'.encode(), mode='0600', owner='pickup:pickup')
                hosts, ports = inv.order_dsn_hosts(self.data)
                dsn = (f'host={hosts} port={ports} dbname={inv.DATABASES[role]} user={inv.owner_user(role)} sslmode=verify-full '
                       f'sslrootcert=/etc/pickup-pact/tls/ca.crt passfile=/dev/shm/pact-migrate/{role}-owner.pgpass '
                       'target_session_attrs=read-write connect_timeout=3')
                command = (f'cd /opt/pickup-pact && sudo -u pickup env PYTHONPATH=/opt/pickup-pact:/opt/pickup-pact/services/reconciler '
                           f'PICKUP_MIGRATE_DSN={shlex.quote(dsn)} /opt/pickup-pact/.venv/bin/python -m demo.route.ha.migrate '
                           f'bump-generation --role {role} --expected {expected} --reason restore-{self.restore_id}')
                done = self.sh(letter, command, check=False, timeout=180)
                if done.returncode:
                    raise AssertionError(f'세대 갱신 실패({role}): '+done.stderr[-300:])
                results.append(json.loads(done.stdout))
        finally:
            self.sh(letter, 'sudo rm -rf /dev/shm/pact-migrate', check=False)
        assert all(item['generation'] == 2 and item['changed'] for item in results), results
        return results

    def internal_transport(self, generation: int):
        from demo.route.ha.transport import TransportPolicy
        directory = self.secrets/'order-mtls'
        if not directory.exists():
            directory.mkdir(mode=0o700)
            for name in ('order.crt', 'order.key'):
                data = self.sh('a', f'sudo cat /etc/pickup-pact/tls/{name}').stdout
                (directory/name).write_text(data)
                (directory/name).chmod(0o600)
        return TransportPolicy.mtls(role='order', allowed_hosts=[f'pact-{x}.pact.internal' for x in HOSTS], ca_file=str(self.ca),
                                    cert_file=str(directory/'order.crt'), key_file=str(directory/'order.key'), generation=generation)


def _scan_for_secrets(report: dict, secret_values: list[str]) -> bool:
    text = json.dumps(report, ensure_ascii=False)
    return not any(value and value in text for value in secret_values)


def full_stage(args) -> dict:
    stage = Path(args.stage)
    if stage.exists():
        subprocess.run(['rm', '-rf', str(stage)], check=True)
    stage.mkdir(mode=0o700)
    dr = FullDR(args.work, stage)
    dr.ensure_hold_dropins()
    api = RealApi(dr)
    rows: dict[str, dict] = {}
    report: dict = dict(scope=SCOPE, timings='시험 환경 측정값이며 운영 RTO/RPO가 아니다')

    def record(name, **facts):
        rows[name] = dict(id=name, passed=True, **facts)

    try:
        began = time.monotonic()
        dr.ensure_units()
        dr.wait_cluster(members=3, timeout=240)
        dr.wait_apps(HOSTS)
        dr.setup_store()
        first = api.start('a')
        completed = api.finish('a', 'b', first)
        pending = api.settle('a', api.start('a', fault='capture_reply_lost'))
        oat = next(plan for plan in pending['all_plans'] if plan['store_id'] == 'oat')
        pending = api.settle('b', api.command('b', pending, 'transfer', quote_id=oat['quote_id']))
        base = dr.base_backup()
        pending = dr.pending_capture(api, pending)
        target = dr.restore_point('pact_target_'+secrets.token_hex(6))
        later = api.call('a', '/api/route/journeys', {})
        bundle = stage/'bundle'
        seal, metadata = dr.collect_bundle(bundle, base=base, target=target)
        # DR-03: 누락·변조·미완성 묶음은 유효한 백업으로 승인하지 않는다(복사본으로 확인).
        copy = stage/'tamper'
        shutil.copytree(bundle, copy)
        victim = next((copy/'wal').iterdir())
        original = victim.read_bytes()
        refused = []
        for label, mutate in [('wal_missing', lambda: victim.unlink()),
                              ('wal_modified', lambda: None)]:
            if label == 'wal_modified':
                victim.write_bytes(bytes([original[0] ^ 1])+original[1:])
            else:
                mutate()
            try:
                verify_bundle(copy, expected_sha256=seal, expected_cluster_id=metadata['cluster_id'])
            except (OSError, ValueError):
                refused.append(label)
        shutil.rmtree(copy)
        assert refused == ['wal_missing', 'wal_modified'], refused
        started = time.monotonic()
        receipt = dr.archive.upload(bundle, expected_sha256=seal, expected_cluster_id=metadata['cluster_id'])
        report['upload_and_readback_s'] = round(time.monotonic()-started, 1)
        controls = dr.negative_controls(receipt, bundle, seal, metadata['cluster_id'])
        record('DR-03', bundles_refused=refused, store_checks=controls)
        record('DR-04', note='저장소 중단·용량 초과 때 영수증 없음(음성 대조). 경보 판정은 backup_ops 시험에서 확인', store_checks=controls)
        vault = dict(receipt=receipt, seal=seal, cluster_id=metadata['cluster_id'])
        (args.output/'receipt.json').write_text(json.dumps(dict(receipt=receipt, seal_sha256=seal), indent=2))
        report.update(required_wal=metadata['required_wal'], target=target['target'], cluster_id=metadata['cluster_id'])
        # DR-08: 운영 주소·시험하지 않는 대상은 파괴 명령이 거부한다(실행하기 전에 확인).
        denied = []
        for target_name, path in [('pickup-pact-demo.onrender.com', '/var/lib/postgresql/17/pickup'), ('host=db.example user=x', '/var/lib/postgresql/17/pickup'),
                                  ('x', '/var/lib/postgresql/17/pickup'), ('a', '/home/ubuntu'), ('a', '/')]:
            try:
                dr.guard.check(target_name, path)
            except PermissionError:
                denied.append(f'{target_name}:{path}')
        assert len(denied) == 5, denied
        record('DR-08', refused=denied)
        # DR-01: 클러스터 데이터·WAL 스풀·etcd 전체 제거, 로컬 묶음 삭제
        destroyed_at = time.monotonic()
        removed = dr.destroy()
        shutil.rmtree(bundle)
        assert not bundle.exists()
        restored = dr.restore(vault['receipt'], vault['seal'], vault['cluster_id'], target['target'])
        report['restore'] = restored
        record('DR-01', removed=removed, restored_members=restored['restored_members'], from_external_store_only=True,
               downloaded_s=restored['downloaded_s'], apps_ready_after_loss_s=round(time.monotonic()-destroyed_at, 1))
        # DR-02: 복원 지점과 거래 집합이 일치하고 이후 기록은 없다.
        done_again = api.call('b', '/api/route/journeys/'+first['id'])
        assert api.proof('a', done_again) == completed, '완료된 주문이 복원 뒤 다르다'
        missing = api.http.get(dr.app_url('a')+'/api/route/journeys/'+later['id'])
        assert missing.status_code == 404, missing.status_code
        record('DR-02', restore_point=target['target'], completed_order_restored=True, post_target_order_absent=True)
        # DR-05: 원래 키로 재개, 재청구 없음
        resumed = api.settle('b', api.call('b', '/api/route/journeys/'+pending['id']), timeout=240)
        proof = api.proof('c', resumed)
        assert proof['capture_count'] == 1 and proof['held_krw'] == 0
        record('DR-05', resumed_with_original_key=proof)
        # DR-06: 이전 세대·이전 토큰 거부, 현재 값은 통과, 기존 거래는 조회 가능
        from demo.route.merchant_http import HttpMerchantFleet
        merchant = 'https://pact-a.pact.internal:8443'
        current = HttpMerchantFleet(merchant, dr.tokens['ROUTE_MERCHANT_TOKEN'], 2, transport=dr.internal_transport(2))
        assert current.snapshot('dr-probe')
        refused6 = []
        for label, token, generation in [('previous_token', dr.previous_tokens['ROUTE_MERCHANT_TOKEN'], 2),
                                         ('previous_generation', dr.tokens['ROUTE_MERCHANT_TOKEN'], 1)]:
            try:
                HttpMerchantFleet(merchant, token, 2, transport=dr.internal_transport(generation)).snapshot('dr-probe')
            except OSError:
                refused6.append(label)
        assert refused6 == ['previous_token', 'previous_generation'], refused6
        record('DR-06', refused=refused6, existing_order_readable=True)
        # DR-07: 키·영수증·봉인값은 DB 호스트 밖(이 시험 도구와 저장소)에만 있었고, 증거에는 비밀값이 없다.
        secret_values = list(dr.passwords.values())+list(dr.tokens.values())+list(dr.previous_tokens.values())+[dr.encryption, dr.writer]
        assert _scan_for_secrets(dict(report=report, rows=rows), secret_values), '증거에 비밀값이 있다'
        record('DR-07', recovered_from='receipt+seal+encryption key held outside the database hosts', secrets_in_evidence=False)
        report['total_s'] = round(time.monotonic()-began, 1)
        report['passed'] = True
    except Exception as exc:  # noqa: BLE001 - 실패도 증거로 남긴다
        report.update(passed=False, error=f'{type(exc).__name__}: {exc}'[:1500])
    finally:
        try:
            dr.teardown_store()
        finally:
            subprocess.run(['rm', '-rf', str(stage), str(dr.secrets)], check=False)
            dr.clear_faults()
            dr.remove_hold_dropins()
    report['scenarios'] = list(rows.values())
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--stage', default='/var/tmp/pact-dr')
    parser.add_argument('--only', nargs='*', default=None)
    parser.add_argument('--repeat', type=int, default=1)
    parser.add_argument('--mode', choices=['backup', 'full'], default='backup')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    report = full_stage(args) if args.mode == 'full' else backup_stage(args)
    scenarios = report.pop('scenarios', None) or [dict(id='DR-backup', **report)]
    scenarios.append(dict(id='DR-summary', **{k: v for k, v in report.items() if k not in {'scenarios'}}))
    (args.output/'results.json').write_text(json.dumps(dict(repetitions=[dict(repeat=1, scenarios=scenarios)],
                                                            passed=report.get('passed', False), scope=SCOPE), ensure_ascii=False, indent=2))
    print(json.dumps({'passed': report.get('passed'), 'scope': SCOPE}), flush=True)
    return 0 if report.get('passed') else 1


if __name__ == '__main__':
    if os.environ.get('PICKUP_HA_TEST') != '1':
        raise SystemExit('명시적 시험 옵트인 필요(PICKUP_HA_TEST=1)')
    raise SystemExit(main())
