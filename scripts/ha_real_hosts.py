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
        done = subprocess.run(self.ssh_args()+['bash', '-lc', shlex.quote(command)], capture_output=True, text=True, timeout=timeout)
        if check and done.returncode:
            raise RuntimeError(f'{self.name}: exit {done.returncode}: {done.stderr.strip()[:500]}')
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
                                                      ssh_ed25519=v.get('ssh_fingerprint')) for k, v in report.items()}),
                     ensure_ascii=False, indent=2))
    return ok


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['preflight'])
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
    return 2


if __name__ == '__main__':
    sys.exit(main())
