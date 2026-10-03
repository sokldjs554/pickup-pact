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
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ha_real_harness import ADDRESSES, HOSTS, RealApi, RealCluster  # noqa: E402
from demo.route.ha.backup import required_wal, seal_bundle  # noqa: E402
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


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--stage', default='/var/tmp/pact-dr')
    parser.add_argument('--only', nargs='*', default=None)
    parser.add_argument('--repeat', type=int, default=1)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    report = backup_stage(args)
    (args.output/'results.json').write_text(json.dumps(dict(repetitions=[dict(repeat=1, scenarios=[dict(id='DR-backup', **report)])],
                                                            passed=report.get('passed', False), scope=SCOPE), ensure_ascii=False, indent=2))
    print(json.dumps({'passed': report.get('passed'), 'scope': SCOPE}), flush=True)
    return 0 if report.get('passed') else 1


if __name__ == '__main__':
    if os.environ.get('PICKUP_HA_TEST') != '1':
        raise SystemExit('명시적 시험 옵트인 필요(PICKUP_HA_TEST=1)')
    raise SystemExit(main())
