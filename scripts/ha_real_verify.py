"""실서버 장애 시험을 실행한다: 시험 도구를 pact-backup에 올리고, 임시 SSH 키로 DB 서버를 조작하게 한다.

- 영구 개인키는 서버에 올리지 않는다. 시험마다 새 키를 만들어 DB 서버에 `from="10.0.0.20"`으로 제한해 등록하고, 끝나면 제거한다.
- 시험 도구가 쓰는 클라이언트 인증서·런타임 passfile은 pact-backup의 메모리 디스크(/dev/shm)에만 두고 끝나면 지운다.
- 장애를 주입하는 단계는 인벤토리 파괴 시험 승인(대상 호스트·기간)을 다시 검사한 뒤에만 실행한다.
"""
from __future__ import annotations
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from demo.route.ha import inventory as inv  # noqa: E402

WORK = '/dev/shm/pact-harness'
BASIC = ['WAL-ARCHIVE', 'HA-01', 'HA-02', 'HA-03']
FAULTS = ['HA-04', 'HA-05', 'HA-06']
UNPARTITION = "sudo iptables-save | grep -v 'pact-partition' | sudo iptables-restore"


def gate(inventory_path: Path, needs_destructive: bool) -> dict:
    data = inv.load(str(inventory_path))
    stage = 'destructive' if needs_destructive else 'deploy'
    problems = inv.validate(data, stage)
    if problems:
        raise SystemExit(f'인벤토리 {stage} 단계 거부: '+'; '.join(problems))
    return data


def verify(hosts, inventory_path: Path, out: Path, *, only: list[str], repeat: int, faults: bool,
           driver: str = 'ha_real_harness.py') -> bool:
    gate(inventory_path, faults)
    if faults:
        approval = ROOT/'infra/ha/hosts/fault-approval.txt'
        if not approval.is_file() or 'approved_by: sokldjs' not in approval.read_text():
            raise SystemExit('장애 주입 시험은 소유자 확인 기록(infra/ha/hosts/fault-approval.txt)이 필요하다')
    by_name = {h.name: h for h in hosts}
    backup, db = by_name['pact-backup'], [h for h in hosts if h.name != 'pact-backup']
    marker = 'pact-harness-'+uuid4().hex[:8]
    keydir = Path(tempfile.mkdtemp(prefix='pact-harness-key-'))
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', marker, '-f', str(keydir/'id')], check=True)
    public = (keydir/'id.pub').read_text().strip()
    out.mkdir(parents=True, exist_ok=True)
    log_lines: list[str] = []
    passed = False
    try:
        # 1. 시험 도구 실행 환경(pact-backup)
        backup.run('sudo apt-get install -y -qq python3-venv >/dev/null && sudo rm -rf /opt/pact-harness && '
                   'sudo mkdir -p /opt/pact-harness && sudo chown ubuntu:ubuntu /opt/pact-harness', timeout=300)
        code = subprocess.run(['git', 'archive', '--format=tar', 'HEAD'], cwd=ROOT, check=True, capture_output=True).stdout
        backup.run_bytes('tar -x -C /opt/pact-harness', code)
        commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()
        backup.run(f'echo {commit} > /opt/pact-harness/.pact-commit')
        if driver == 'ha_real_dr.py':
            # 인벤토리가 쓰는 백업 저장소 이름을 모든 서버에서 해석할 수 있게 한다(이미 설치된 서버용).
            for host in hosts:
                host.run("grep -q ' backup\\.pact\\.internal' /etc/hosts || sudo sed -i 's/^\\(10\\.0\\.0\\.20 .*\\)$/\\1 backup.pact.internal/' /etc/hosts")
        if driver == 'ha_real_dr.py':  # 기본 백업(pg_basebackup)에 필요한 PostgreSQL 17 클라이언트
            backup.run('sudo apt-get install -y -qq postgresql-common gnupg >/dev/null && '
                       'sudo /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y >/dev/null 2>&1 && sudo apt-get update -qq && '
                       'sudo apt-get install -y -qq --no-install-recommends postgresql-client-17 >/dev/null', timeout=600)
        backup.run('python3 -m venv /opt/pact-harness/.venv && /opt/pact-harness/.venv/bin/pip install -q --disable-pip-version-check '
                   '-r /opt/pact-harness/requirements-ha.txt', timeout=900)
        # 2. 비공개 자료: 메모리 디스크에만 둔다.
        backup.run(f'rm -rf {WORK} && umask 077 && mkdir -p {WORK}/out')
        source = db[0]
        files = {'ca.crt': '/etc/pickup-pact/tls/ca.crt', 'harness.crt': '/etc/pickup-pact/tls/etcd-client.crt',
                 'harness.key': '/etc/pickup-pact/tls/etcd-client.key',
                 'order-runtime.pgpass': '/etc/pickup-pact/secrets/order.pgpass'}
        for name, path in files.items():
            data = source.run(f'sudo cat {path}').stdout
            backup.run_bytes(f'umask 077 && cat > {WORK}/{name}', data.encode())
        backup.run_bytes(f'umask 077 && cat > {WORK}/id', (keydir/'id').read_bytes())
        # 3. DB 서버에는 시험 도구 주소로 제한한 공개키만 등록한다.
        entry = f'from="10.0.0.20",no-port-forwarding,no-agent-forwarding,no-X11-forwarding {public}'
        for host in db:
            # 장애 시험에서 호스트가 즉시 재부팅(동기화 없음)되어도 시험 도구의 접속 키가 남도록 디스크에 확정한다.
            host.run(f"echo {shlex.quote(entry)} >> ~/.ssh/authorized_keys && sync")
        # 시험 도구가 DB 서버 세 대에 모두 접속할 수 있는지 먼저 확인한다(안 되면 시작하지 않는다).
        reach = {}
        for host in db:
            done = backup.run(f"ssh -i {WORK}/id -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new "
                              f"-o UserKnownHostsFile={WORK}/known_hosts -o ConnectTimeout=8 ubuntu@{host.spec['private']} 'echo ok' 2>&1 | tail -1",
                              check=False, timeout=60)
            reach[host.name] = done.stdout.strip()[:120]
        (out/'ssh-check.json').write_text(json.dumps(reach, ensure_ascii=False, indent=2))
        print('[ssh-check]', json.dumps(reach, ensure_ascii=False), flush=True)
        if any(value != 'ok' for value in reach.values()):
            raise AssertionError('시험 도구가 DB 서버에 접속하지 못한다: '+json.dumps(reach, ensure_ascii=False))
        args = ['--work', WORK, '--output', f'{WORK}/out', '--repeat', str(repeat)]
        if only:
            args += ['--only', *only]
        script = (f'cd /opt/pact-harness && PYTHONPATH=.:services/reconciler PICKUP_HA_TEST=1 '
                  f'/opt/pact-harness/.venv/bin/python scripts/{driver} {" ".join(args)} > {WORK}/log.txt 2>&1; echo $? > {WORK}/exit')
        backup.run_bytes(f'cat > {WORK}/run.sh', script.encode())
        backup.run(f'nohup setsid bash {WORK}/run.sh >/dev/null 2>&1 < /dev/null & echo started')
        # 4. 진행 상황을 따라가며 기다린다.
        deadline, seen = time.monotonic()+110*60, 0
        while time.monotonic() < deadline:
            time.sleep(20)
            text = backup.run(f'cat {WORK}/log.txt 2>/dev/null; true', check=False, timeout=60).stdout.splitlines()
            for line in text[seen:]:
                print('[harness]', line[:400], flush=True)
            seen = len(text)
            if backup.run(f'test -f {WORK}/exit && cat {WORK}/exit', check=False, timeout=60).stdout.strip():
                break
        else:
            raise AssertionError('시험 도구가 제한 시간 안에 끝나지 않았다')
        results = backup.run(f'cat {WORK}/out/results.json', timeout=60).stdout
        (out/'results.json').write_text(results)
        log_lines = backup.run(f'cat {WORK}/log.txt', check=False, timeout=60).stdout.splitlines()
        (out/'harness.log').write_text('\n'.join(log_lines)+'\n')
        passed = bool(json.loads(results).get('passed'))
        post = {}
        for host in db:
            try:
                post[host.name] = host.run(f"echo keys={{$(grep -c {marker} ~/.ssh/authorized_keys)}} boot=$(uptime -s) "
                                           f"mtime=$(stat -c %y ~/.ssh/authorized_keys | cut -c1-19)", check=False, timeout=40).stdout.strip()
            except Exception as exc:  # noqa: BLE001
                post[host.name] = f'unreachable: {type(exc).__name__}'
        (out/'post-state.json').write_text(json.dumps(post, ensure_ascii=False, indent=2))
    finally:
        # 5. 정리: 임시 키 제거, 분리 규칙 제거, 비공개 자료 삭제 (서버가 재부팅 중이면 기다린다).
        for host in db:
            for _ in range(30):
                try:
                    host.run(f"sed -i '/{marker}/d' ~/.ssh/authorized_keys && {UNPARTITION}", timeout=40)
                    break
                except Exception:  # noqa: BLE001
                    time.sleep(10)
        try:
            backup.run(f'pkill -f ha_real_harness.py || true; rm -rf {WORK}', check=False, timeout=60)
        except Exception:  # noqa: BLE001
            pass
        subprocess.run(['rm', '-rf', str(keydir)], check=False)
    return passed
