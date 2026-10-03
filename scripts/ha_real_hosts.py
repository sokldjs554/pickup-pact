#!/usr/bin/env python3
"""승인된 독립 호스트에 SSH로 접속해 점검·배포·시험을 실행한다.

개인키는 환경 변수 PACT_HA_SSH_KEY_FILE이 가리키는 파일(0600)에서만 읽고, 출력·증거에 남기지 않는다.
이 스크립트는 infra/ha/hosts/*.json에 적힌 호스트에만 접속한다.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))


def load(path: Path) -> dict:
    return json.loads(path.read_text())


class Host:
    def __init__(self, spec: dict, user: str, key: str, known_hosts: str):
        self.spec, self.user, self.key, self.known_hosts = spec, user, key, known_hosts
        self.name = spec['name']

    def ssh_args(self) -> list[str]:
        return ['ssh', '-i', self.key, '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
                '-o', 'StrictHostKeyChecking=accept-new', '-o', f'UserKnownHostsFile={self.known_hosts}',
                '-o', 'ConnectTimeout=20', '-o', 'ServerAliveInterval=15', f"{self.user}@{self.spec['public']}"]

    def run(self, command: str, *, timeout=300, check=True) -> subprocess.CompletedProcess:
        done = subprocess.run(self.ssh_args()+['bash', '-lc', shlex.quote(command)], capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL)
        if check and done.returncode:
            raise RuntimeError(f'{self.name}: exit {done.returncode}: {done.stderr.strip()[:500]}')
        return done


    def run_bytes(self, command: str, data: bytes, *, timeout=300) -> subprocess.CompletedProcess:
        """표준 입력으로 바이트(비밀값 포함)를 전달한다. 명령행에는 값이 나오지 않는다."""
        done = subprocess.run(self.ssh_args()+['bash', '-lc', shlex.quote(command)], input=data, capture_output=True, timeout=timeout)
        done.stdout, done.stderr = done.stdout.decode(errors='replace'), done.stderr.decode(errors='replace')
        if done.returncode and not command.startswith('sudo -u postgres psql'):
            raise RuntimeError(f'{self.name}: exit {done.returncode}: {done.stderr.strip()[-500:]}')
        return done

    def run_script(self, script: Path, args: list[str], *, timeout=1500) -> subprocess.CompletedProcess:
        done = subprocess.run(self.ssh_args()+['sudo', 'bash', '-s', '--']+args, input=script.read_text(),
                              capture_output=True, text=True, timeout=timeout)
        if done.returncode:
            raise RuntimeError(f'{self.name}: install exit {done.returncode}: {done.stderr.strip()[-1500:]}')
        return done


PROBE = r'''
set -u
echo "os=$(. /etc/os-release; echo $PRETTY_NAME)"
echo "arch=$(uname -m)"
echo "kernel=$(uname -r)"
echo "cpus=$(nproc)"
echo "mem_mib=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)"
echo "disk_free_gib=$(df -BG --output=avail / | tail -1 | tr -dc 0-9)"
echo "private_ips=$(hostname -I)"
echo "sudo=$(sudo -n true 2>/dev/null && echo yes || echo no)"
echo "ntp_synced=$(timedatectl show -p NTPSynchronized --value 2>/dev/null)"
echo "softdog=$(sudo -n modprobe softdog 2>/dev/null && echo loadable || echo missing)"
echo "ufw=$(sudo -n ufw status 2>/dev/null | head -1)"
meta() { curl -sf -m 5 -H 'Authorization: Bearer Oracle' "http://169.254.169.254/opc/v2/instance/$1"; }
echo "fault_domain=$(meta faultDomain)"
echo "availability_domain=$(meta availabilityDomain)"
echo "region=$(meta canonicalRegionName)"
echo "shape=$(meta shape)"
echo "ssh_fingerprint=$(ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub 2>/dev/null | cut -d' ' -f2)"
'''


def preflight(hosts: list[Host], inventory: dict, out: Path) -> bool:
    ok = True
    report = {}
    for host in hosts:
        row = dict(x.split('=', 1) for x in host.run(PROBE).stdout.splitlines() if '=' in x)
        problems = []
        if row.get('arch') != 'x86_64':
            problems.append('x86_64가 아님')
        if row.get('sudo') != 'yes':
            problems.append('비밀번호 없는 sudo 불가')
        if host.spec['private'] not in row.get('private_ips', '').split():
            problems.append(f"private IP 불일치: {row.get('private_ips')}")
        if int(row.get('mem_mib', '0')) < 900 and host.name != 'pact-backup':
            problems.append('메모리 부족')
        if host.name != 'pact-backup' and row.get('softdog') != 'loadable':
            problems.append('softdog 모듈 사용 불가')
        if host.spec.get('fault_domain', 'unspecified') != 'unspecified' and row.get('fault_domain') != host.spec['fault_domain']:
            problems.append(f"fault domain 불일치: {row.get('fault_domain')}")
        row['problems'] = problems
        report[host.name] = row
        ok &= not problems
    # 호스트 사이 사설망 도달성. Oracle 기본 규칙은 ICMP echo를 막으므로 TCP 22로 확인한다.
    for src in hosts:
        reach = {}
        for dst in hosts:
            if dst is src:
                continue
            done = src.run(f"timeout 4 bash -c 'exec 3<>/dev/tcp/{dst.spec['private']}/22' >/dev/null 2>&1 && echo up || echo down", check=False)
            reach[dst.name] = done.stdout.strip()
            ok &= reach[dst.name] == 'up'
        report[src.name]['private_tcp22'] = reach
    out.mkdir(parents=True, exist_ok=True)
    (out/'preflight.json').write_text(json.dumps(dict(inventory=inventory['name'], scope=inventory['scope'], passed=ok, hosts=report),
                                                 ensure_ascii=False, indent=2))
    print(json.dumps(dict(passed=ok, hosts={k: dict(arch=v.get('arch'), cpus=v.get('cpus'), mem_mib=v.get('mem_mib'),
                                                      disk_gib=v.get('disk_free_gib'), problems=v['problems'], tcp22=v.get('private_tcp22'),
                                                      fault_domain=v.get('fault_domain'), ad=v.get('availability_domain'), region=v.get('region'), shape=v.get('shape'), ssh_ed25519=v.get('ssh_fingerprint')) for k, v in report.items()}),
                     ensure_ascii=False, indent=2))
    return ok


def install(hosts: list[Host], inventory: dict, out: Path) -> bool:
    from concurrent.futures import ThreadPoolExecutor
    cidr = inventory['private_cidr']
    pairs = ','.join(f"{h.spec['private']}={h.name}" for h in hosts)
    script = ROOT/'infra/ha/hosts/install.sh'

    def one(host):
        role = 'backup' if host.name == 'pact-backup' else 'db'
        try:
            done = host.run_script(script, [role, cidr, pairs])
            facts = dict(x.split('=', 1) for x in done.stdout.splitlines() if '=' in x)
            return host.name, dict(passed=True, **facts)
        except Exception as exc:  # noqa: BLE001 - 서버별 실패를 모두 모은다
            return host.name, dict(passed=False, error=str(exc)[:1800])

    with ThreadPoolExecutor(max_workers=len(hosts)) as pool:
        report = dict(pool.map(one, hosts))
    ok = all(row['passed'] for row in report.values())
    out.mkdir(parents=True, exist_ok=True)
    (out/'install.json').write_text(json.dumps(dict(inventory=inventory['name'], scope=inventory['scope'], passed=ok, hosts=report),
                                                ensure_ascii=False, indent=2))
    print(json.dumps(dict(passed=ok, hosts=report), ensure_ascii=False, indent=2))
    return ok


DIAGNOSE = r'''
echo "== units"; systemctl is-active etcd patroni 2>&1 | tr '\n' ' '; echo
echo "== etcd state"; systemctl show etcd -p ActiveState -p SubState -p Result -p NRestarts -p ExecMainStatus | tr '\n' ' '; echo
echo "== etcd warnings"; sudo journalctl -u etcd --no-pager -p warning -o cat 2>&1 | grep -v "prober detected" | tail -6 | cut -c1-420
echo "== etcd dial errors"; sudo journalctl -u etcd --no-pager -o cat 2>&1 | grep -o 'dial tcp [0-9.:]*: [a-z ]*[a-z:]*' | sort | uniq -c | head -6
echo "== peer reachability"; for ip in 10.0.0.11 10.0.0.12 10.0.0.13; do timeout 3 bash -c "exec 3<>/dev/tcp/$ip/2380" 2>/dev/null && echo "$ip:2380 open" || echo "$ip:2380 closed"; done
echo "== listening"; sudo ss -ltn | awk '$4 ~ /:(2379|2380|5432|8008)$/ {print $4}' | sort | tr '\n' ' '; echo
echo "== port probe (open=열림, refused=도달했지만 대기 프로세스 없음, blocked=네트워크에서 차단)"
for ip in 10.0.0.11 10.0.0.12 10.0.0.13 10.0.0.20; do
  [ "$ip" = "$(hostname -I | awk '{print $1}')" ] && continue
  row="$ip"
  for port in 22 2379 2380 5432 8008 8000 8443 8444 8445; do
    start=$(date +%s%N)
    if timeout 3 bash -c "exec 3<>/dev/tcp/$ip/$port" 2>/dev/null; then kind=open
    else ms=$(( ($(date +%s%N)-start)/1000000 )); if [ "$ms" -lt 800 ]; then kind=refused; else kind=blocked; fi; fi
    row="$row $port=$kind"
  done
  echo "$row"
done
echo "== boot"; uptime -s; ls -l /dev/watchdog 2>&1 | cut -c1-80; lsmod | grep -c softdog
echo "== patroni state"; systemctl show patroni -p ActiveState -p SubState -p Result -p ExecMainStatus | tr '\n' ' '; echo
echo "== patroni journal"; sudo journalctl -u patroni --no-pager -b -n 14 -o cat 2>&1 | cut -c1-300
echo "== softdog boot config"; cat /etc/modules-load.d/softdog.conf 2>&1 | head -2; sudo journalctl -b -u systemd-modules-load --no-pager -o cat 2>&1 | tail -4 | cut -c1-200
echo "== authorized_keys harness lines"; grep -c pact-harness ~/.ssh/authorized_keys
echo "== etcd env"; grep -E "ETCD_(NAME|LISTEN|INITIAL_ADVERTISE)" /etc/pickup-pact/etcd.env
'''


def diagnose(hosts: list[Host], out: Path) -> bool:
    report = {h.name: h.run(DIAGNOSE, check=False, timeout=120).stdout for h in hosts if h.name != 'pact-backup'}
    out.mkdir(parents=True, exist_ok=True)
    (out/'diagnose.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    for name, text in report.items():
        print(f'##### {name}\n{text}')
    return True


def refresh_units(hosts: list[Host], out: Path) -> bool:
    """수정한 systemd 유닛을 DB 서버에 올리고, 멈춘 유닛을 시작해 3노드로 되돌린다(데이터는 건드리지 않는다)."""
    import io
    import tarfile
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as archive:
        for unit in sorted((ROOT/'infra/ha/systemd').glob('*.service')):
            raw = unit.read_bytes()
            info = tarfile.TarInfo(f'etc/systemd/system/{unit.name}')
            info.size, info.mode = len(raw), 0o644
            archive.addfile(info, io.BytesIO(raw))
    report = {}
    units = ['etcd', 'patroni', 'pickup-merchant', 'pickup-order-notification', 'pickup-payment', 'pickup-order-app', 'pickup-order-worker']
    for host in [h for h in hosts if h.name != 'pact-backup']:
        host.run_bytes('sudo tar -xp -C /', buffer.getvalue())
        host.run('sudo systemctl daemon-reload')
        started = []
        for unit in units:
            if host.run(f'systemctl is-active --quiet {unit}', check=False).returncode:
                host.run(f'sudo systemctl start {unit}', check=False, timeout=150)
                started.append(unit)
        report[host.name] = dict(started=started, states=host.run('systemctl is-active '+' '.join(units), check=False).stdout.split())
    deadline = time.monotonic()+240
    members = None
    while time.monotonic() < deadline:
        done = hosts[0].run('sudo -u postgres /opt/patroni/bin/patronictl -c /etc/pickup-pact/patroni.yml list -f json', check=False, timeout=60)
        if done.returncode == 0 and done.stdout.strip():
            members = [(m['Member'], m['Role'], m['State']) for m in json.loads(done.stdout)]
            if len(members) == 3 and all(m[2] in {'running', 'streaming'} for m in members):
                break
        time.sleep(5)
    ok = bool(members) and len(members) == 3 and all(m[2] in {'running', 'streaming'} for m in members)
    out.mkdir(parents=True, exist_ok=True)
    (out/'refresh-units.json').write_text(json.dumps(dict(passed=ok, hosts=report, members=members), ensure_ascii=False, indent=2))
    print(json.dumps(dict(passed=ok, hosts=report, members=members), ensure_ascii=False))
    return ok


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['preflight', 'install', 'deploy', 'diagnose', 'verify-basic', 'verify-faults', 'refresh-units'])
    parser.add_argument('--inventory', type=Path, default=ROOT/'infra/ha/hosts/oracle-osaka.json')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    key = os.environ.get('PACT_HA_SSH_KEY_FILE')
    if not key or not Path(key).is_file():
        raise SystemExit('PACT_HA_SSH_KEY_FILE이 필요하다')
    if Path(key).stat().st_mode & 0o077:
        raise SystemExit('개인키 파일 권한은 0600이어야 한다')
    inventory = load(args.inventory)
    known = str(args.output/'known_hosts')
    args.output.mkdir(parents=True, exist_ok=True)
    hosts = [Host(spec, inventory['ssh_user'], key, known) for spec in inventory['hosts']]
    if args.phase == 'preflight':
        return 0 if preflight(hosts, inventory, args.output) else 1
    if args.phase == 'install':
        return 0 if install(hosts, inventory, args.output) else 1
    if args.phase.startswith('verify-'):
        from ha_real_verify import verify, BASIC, FAULTS
        faults = args.phase == 'verify-faults'
        try:
            ok = verify(hosts, ROOT/'infra/ha/inventory.oracle-osaka.yaml', args.output,
                        only=FAULTS if faults else BASIC, repeat=int(os.environ.get('PACT_REPEAT') or (ROOT/'infra/ha/hosts/repeat.txt').read_text().strip() or 1), faults=faults)
        except BaseException as exc:  # noqa: BLE001
            print(json.dumps(dict(passed=False, error=f'{type(exc).__name__}: {str(exc)[:800]}'), ensure_ascii=False))
            return 1
        print(json.dumps(dict(passed=ok)))
        return 0 if ok else 1
    if args.phase == 'refresh-units':
        return 0 if refresh_units(hosts, args.output) else 1
    if args.phase == 'diagnose':
        return 0 if diagnose(hosts, args.output) else 1
    if args.phase == 'deploy':
        from ha_real_deploy import Deployment
        try:
            result = Deployment(hosts, ROOT/'infra/ha/inventory.oracle-osaka.yaml', args.output).run()
        except BaseException as exc:  # noqa: BLE001
            print(json.dumps(dict(passed=False, error=f'{type(exc).__name__}: {str(exc)[:800]}'), ensure_ascii=False))
            return 1
        print(json.dumps(dict(passed=result['passed'], seconds=result['seconds'], scope=result['scope'])))
        return 0 if result['passed'] else 1
    return 2


if __name__ == '__main__':
    sys.exit(main())
