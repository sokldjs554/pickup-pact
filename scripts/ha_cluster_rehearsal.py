#!/usr/bin/env python3
"""Single-host rehearsal of the rendered ha_postgres_v1 deployment (Patroni + etcd).

Three Docker "host" containers stand in for three hosts on ONE machine. Each
owns a network namespace on a cluster network (etcd, Patroni, replication,
internal mTLS) and a client network (application -> PostgreSQL). etcd,
Patroni and every service role join that namespace, using the files rendered
from the inventory and the systemd ExecStart lines verbatim.

Every container, volume and network is created here, labelled with the run id
and removed only after the label is checked. Passwords, tokens and private keys
stay in a 0700 private directory that is deleted on close and never copied to
the evidence directory. This is development evidence for the configuration and
failover behaviour: it is not independent-host HA, and timings are not RTO/RPO.
"""
from __future__ import annotations
import ipaddress
import json
import os
from pathlib import Path
import random
import re
import secrets
import shlex
import shutil
import ssl
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'scripts'))

from demo.route.ha import inventory as inv  # noqa: E402
from demo.route.ha.scram import verifier  # noqa: E402
from ha_rehearsal_pki import PrivateAuthority  # noqa: E402

PATRONI_IMAGE = 'pickup-patroni-rehearsal:dev'
APP_IMAGE = 'pickup-app-rehearsal:dev'
ETCD_IMAGE = 'gcr.io/etcd-development/etcd:v3.5.17'
LABEL = 'pickup.ha-rehearsal'
HOSTS = ('a', 'b', 'c')
SERVICE_UID, POSTGRES_UID = 10001, 999
ROLES = ('order', 'merchant', 'payment')
SERVICES = {  # container suffix -> (rendered env role, systemd unit)
    'merchant': ('merchant', 'pickup-merchant.service'),
    'payment': ('payment', 'pickup-payment.service'),
    'notification': ('order', 'pickup-order-notification.service'),
    'app': ('order', 'pickup-order-app.service'),
    'worker': ('order', 'pickup-order-worker.service'),
}
START_ORDER = ('merchant', 'notification', 'payment', 'app', 'worker')


def docker_env_file(values: dict, path: Path) -> None:
    """docker --env-file keeps quotes literally, so write raw KEY=VALUE lines."""
    for key, value in values.items():
        if '\n' in value or '\r' in value:
            raise ValueError('multi-line environment values are not supported')
    path.write_text(''.join(f'{key}={value}\n' for key, value in values.items()))
    os.chmod(path, 0o600)


def systemd_env(path: Path) -> dict:
    values = {}
    for line in path.read_text().splitlines():
        if not line or line.startswith('#'):
            continue
        key, value = line.split('=', 1)
        values[key] = shlex.split(value)[0] if value.startswith("'") else value
    return values


def unit_command(unit: str, env: dict) -> list[str]:
    text = (ROOT/'infra/ha/systemd'/unit).read_text()
    line = next(row for row in text.splitlines() if row.startswith('ExecStart='))
    words = shlex.split(line[len('ExecStart='):])
    words = ['python' if word == '/opt/pickup-pact/.venv/bin/python' else word for word in words]
    return [re.sub(r'\$\{([A-Z0-9_]+)\}', lambda match: env[match.group(1)], word) for word in words]


class ClusterRehearsal:
    def __init__(self, root: Path):
        if os.environ.get('PICKUP_HA_TEST') != '1':
            raise ValueError('explicit isolated rehearsal opt-in required (PICKUP_HA_TEST=1)')
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=False)
        self.run = uuid4().hex[:12]
        self.prefix = f'pickup-ha-{self.run}-'
        self.private = Path(tempfile_dir()) / ('pickup-ha-'+self.run)
        self.private.mkdir(mode=0o700)
        self.evidence = self.root/'evidence'
        self.evidence.mkdir()
        self.config = self.root/'config'
        self.secrets: list[str] = []
        self.created: dict[str, list[str]] = {'container': [], 'volume': [], 'network': []}
        self.passwords = {name: secrets.token_hex(24) for name in
                          ['superuser', 'replication', 'rewind', *[f'{r}_owner' for r in ROLES],
                           *[f'{r}_runtime' for r in ROLES]]}
        self.tokens = {'ROUTE_MERCHANT_TOKEN': secrets.token_hex(32), 'ROUTE_PAYMENT_TOKEN': secrets.token_hex(32),
                       'ROUTE_PAYMENT_NOTIFY_SECRET': secrets.token_hex(32)}
        self.secrets += list(self.passwords.values()) + list(self.tokens.values())
        self.timeline: list[dict] = []

    # ------------------------------------------------------------ docker access
    def docker(self, *args, check=True, timeout=180, stdin: str | None = None):
        result = subprocess.run(['docker', *map(str, args)], input=stdin, text=True, capture_output=True, timeout=timeout)
        if check and result.returncode:
            raise RuntimeError('docker '+str(args[0])+' failed: '+self.redact(result.stderr)[-1500:])
        return result

    def redact(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, '[redacted]')
        return re.sub(r'SCRAM-SHA-256\$[^\s\']+', 'SCRAM-SHA-256$[redacted]', text)

    def name(self, *parts) -> str:
        return self.prefix + '-'.join(parts)

    def create(self, kind: str, *args):
        name = args[-1] if kind in {'volume', 'network'} else None
        if kind == 'volume':
            self.docker('volume', 'create', '--label', f'{LABEL}={self.run}', name)
        elif kind == 'network':
            self.docker('network', 'create', '--label', f'{LABEL}={self.run}', *args)
        self.created[kind].append(name)
        return name

    def owned(self, kind: str, name: str) -> bool:
        if not name.startswith(self.prefix):
            return False
        result = self.docker(kind, 'inspect', name, check=False)
        if result.returncode:
            return False
        info = json.loads(result.stdout)[0]
        labels = info.get('Labels') if kind in {'volume', 'network'} else info.get('Config', {}).get('Labels')
        return (labels or {}).get(LABEL) == self.run

    def run_container(self, name: str, *args, image: str, command=()):
        self.docker('run', '-d', '--name', name, '--label', f'{LABEL}={self.run}', *args, image, *command)
        if name not in self.created['container']:
            self.created['container'].append(name)

    # ----------------------------------------------------------- configuration
    def _subnets(self):
        used = set()
        for network in self.docker('network', 'ls', '-q').stdout.split():
            for config in json.loads(self.docker('network', 'inspect', network).stdout)[0].get('IPAM', {}).get('Config') or []:
                if config.get('Subnet'):
                    used.add(ipaddress.ip_network(config['Subnet'], strict=False))
        for _ in range(200):
            third = random.randint(16, 250)
            pair = (ipaddress.ip_network(f'172.28.{third}.0/24'), ipaddress.ip_network(f'172.27.{third}.0/24'))
            if not any(net.overlaps(other) for net in pair for other in used):
                return pair
        raise RuntimeError('no free private subnet pair for the rehearsal networks')

    def inventory(self) -> dict:
        cluster, client = self.cluster_net, self.client_net
        hosts = []
        for index, letter in enumerate(HOSTS):
            hosts.append({'name': f'pact-{letter}', 'dns': f'pact-{letter}.ha.test',
                          'address': str(cluster.network_address+11+index),
                          'client_address': str(client.network_address+11+index),
                          'physical_host': 'single-docker-host', 'failure_domain': 'single-docker-host',
                          'roles': ['postgres', 'etcd', 'order', 'merchant', 'payment'] + (['entry'] if index < 2 else [])})
        return {
            'schema': inv.SCHEMA, 'kind': 'single_host_rehearsal', 'name': 'pact-rehearsal-'+self.run[:6],
            'hosts': hosts,
            'postgres': {'version': '17.11', 'synchronous_node_count': 1, 'watchdog': 'off',
                         'image': 'postgres:17.11-bookworm@sha256:639ab7ceb90e13123085b741fb31ef493fba25463002f6da665352e7b534b652',
                         'client_cidrs': [str(cluster), str(client)]},
            'entry': {'method': 'dns_multi_a', 'public_name': 'pickup.ha.test', 'hosts': ['pact-a', 'pact-b']},
            'backup': {'host': {'name': 'pact-backup', 'dns': 'backup.ha.test', 'address': str(cluster.network_address+20),
                                'physical_host': 'single-docker-host', 'failure_domain': 'single-docker-host'},
                       'repository_url': 'https://backup.ha.test:8000/pickup-pact', 'writer': 'append_only',
                       'maintainer_credential_location': 'rehearsal-operator', 'key_escrow': ['rehearsal-operator'],
                       'receipt_locations': ['rehearsal-operator'], 'base_backup_interval_hours': 24,
                       'max_base_backup_age_hours': 30, 'retain_base_backups': 2, 'max_wal_archive_delay_seconds': 300,
                       'capacity_gib': 10, 'alert_free_ratio': 0.2},
        }

    def host(self, letter):
        return next(h for h in inv.hosts_of(self.data) if h.name == f'pact-{letter}')

    def _pki(self):
        ca = PrivateAuthority(self.private/'pki')
        self.ca = ca
        self.harness_cert = ca.issue('harness', role=None, dns=('harness.ha.test',), ips=())
        self.etcd_client = ca.issue('etcd-client', role=None, dns=('etcd-client.ha.test',), ips=())
        for letter in HOSTS:
            host = self.host(letter)
            out = self.private/f'tls-{letter}'
            out.mkdir(mode=0o700)
            shutil.copy(ca.cert, out/'ca.crt')
            server = dict(dns=(host.dns,), ips=(host.address, host.client_address))
            files = {'postgres': ca.issue(f'{letter}-postgres', role=None, **server),
                     'patroni': ca.issue(f'{letter}-patroni', role=None, **server),
                     'etcd': ca.issue(f'{letter}-etcd', role=None, **server),
                     'etcd-client': self.etcd_client}
            for role in ROLES:
                files[role] = ca.issue(f'{letter}-{role}', role=role, **server)
            for target, (cert, key) in files.items():
                shutil.copy(cert, out/f'{target}.crt')
                shutil.copy(key, out/f'{target}.key')

    def _volumes(self, letter):
        owners = {'postgres': POSTGRES_UID, 'patroni': POSTGRES_UID, 'etcd-client': POSTGRES_UID, 'etcd': 0,
                  'order': SERVICE_UID, 'merchant': SERVICE_UID, 'payment': SERVICE_UID}
        for kind in ('tls', 'secrets', 'data', 'spool', 'etcd'):
            self.create('volume', self.name(letter, kind))
        host = self.host(letter)
        hosts, ports = inv.order_dsn_hosts(self.data)
        pgpass = self.private/f'secrets-{letter}'
        pgpass.mkdir(mode=0o700)
        for role in ROLES:
            (pgpass/f'{role}.pgpass').write_text(f'*:*:*:{inv.runtime_user(role)}:{self.passwords[role+"_runtime"]}\n')
        script = ['set -e', 'cp /src-tls/* /tls/', 'chmod 0644 /tls/*.crt', 'chmod 0600 /tls/*.key']
        script += [f'chown {uid}:{uid} /tls/{name}.key' for name, uid in owners.items()]
        script += ['cp /src-secrets/* /secrets/', f'chown {SERVICE_UID}:{SERVICE_UID} /secrets/*', 'chmod 0600 /secrets/*',
                   f'chown {POSTGRES_UID}:{POSTGRES_UID} /data /spool', 'chmod 0700 /data /spool']
        self.docker('run', '--rm', '--network', 'none', '-v', f'{self.private}/tls-{letter}:/src-tls:ro',
                    '-v', f'{pgpass}:/src-secrets:ro', '-v', f'{self.name(letter, "tls")}:/tls',
                    '-v', f'{self.name(letter, "secrets")}:/secrets', '-v', f'{self.name(letter, "data")}:/data',
                    '-v', f'{self.name(letter, "spool")}:/spool', APP_IMAGE, 'sh', '-c', ' && '.join(script))
        del host, hosts, ports

    # ----------------------------------------------------------------- startup
    def setup(self):
        self.cluster_net, self.client_net = self._subnets()
        self.create('network', '--subnet', str(self.cluster_net), self.name('cluster'))
        self.create('network', '--subnet', str(self.client_net), self.name('client'))
        self.data = self.inventory()
        problems = inv.validate(self.data, 'review')
        if problems:
            raise AssertionError('rehearsal inventory rejected: '+'; '.join(problems))
        self.manifest = inv.render(self.data, self.config)
        self._pki()
        patroni_env = self.private/'patroni.env'
        docker_env_file({'PATRONI_SUPERUSER_PASSWORD': self.passwords['superuser'],
                         'PATRONI_REPLICATION_PASSWORD': self.passwords['replication'],
                         'PATRONI_REWIND_PASSWORD': self.passwords['rewind'],
                         'PICKUP_WAL_SPOOL': '/var/lib/pickup-pact/wal-spool'}, patroni_env)
        for role in ROLES:
            wanted = {'order': ['ROUTE_MERCHANT_TOKEN', 'ROUTE_PAYMENT_TOKEN', 'ROUTE_PAYMENT_NOTIFY_SECRET'],
                      'merchant': ['ROUTE_MERCHANT_TOKEN'],
                      'payment': ['ROUTE_PAYMENT_TOKEN', 'ROUTE_PAYMENT_NOTIFY_SECRET']}[role]
            docker_env_file({k: self.tokens[k] for k in wanted}, self.private/f'{role}-secrets.env')
        for letter in HOSTS:
            self._volumes(letter)
            self.start_namespace(letter)
        for letter in HOSTS:
            self.start_etcd(letter)
        self.wait_etcd()
        for letter in HOSTS:
            self.start_patroni(letter)
        self.wait_cluster(members=3)
        self.bootstrap_roles()
        for letter in HOSTS:
            for service in START_ORDER:
                self.start_service(letter, service)
        self.wait_apps(HOSTS)

    def start_namespace(self, letter):
        host = self.host(letter)
        name = self.name(letter, 'host')
        if name in self.created['container']:
            self.docker('start', name)
            return
        # Names resolve to the cluster interface for every role sharing this namespace.
        pins = [arg for member in inv.hosts_of(self.data) for arg in ('--add-host', f'{member.dns}:{member.address}')]
        self.run_container(name, '--hostname', host.dns, '--network', self.name('cluster'), '--ip', host.address,
                           *pins, image=APP_IMAGE, command=('sleep', 'infinity'))
        self.docker('network', 'connect', '--ip', host.client_address, self.name('client'), name)

    def start_etcd(self, letter):
        name = self.name(letter, 'etcd')
        if name in self.created['container']:
            self.docker('start', name)
            return
        env = self.private/f'etcd-{letter}.env'
        docker_env_file(systemd_env(self.config/f'hosts/pact-{letter}/etcd.env'), env)
        self.run_container(name, '--network', f'container:{self.name(letter, "host")}', '--env-file', env,
                           '-v', f'{self.name(letter, "tls")}:/etc/pickup-pact/tls:ro',
                           '-v', f'{self.name(letter, "etcd")}:/var/lib/etcd', image=ETCD_IMAGE,
                           command=('/usr/local/bin/etcd',))

    def etcdctl(self, letter, *args, check=True, timeout=30):
        return self.docker('exec', self.name(letter, 'etcd'), '/usr/local/bin/etcdctl',
                           f'--endpoints=https://pact-{letter}.ha.test:2379', '--cacert=/etc/pickup-pact/tls/ca.crt',
                           '--cert=/etc/pickup-pact/tls/etcd-client.crt', '--key=/etc/pickup-pact/tls/etcd-client.key',
                           *args, check=check, timeout=timeout)

    def wait_etcd(self, timeout=90):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            result = self.etcdctl('a', 'endpoint', 'health', '--cluster', '-w', 'json', check=False)
            if result.returncode == 0:
                health = json.loads(result.stdout)
                if len(health) == 3 and all(item.get('health') for item in health):
                    return
            time.sleep(1)
        raise AssertionError('etcd quorum did not become healthy')

    def start_patroni(self, letter):
        name = self.name(letter, 'patroni')
        if name in self.created['container']:
            self.docker('start', name)
            return
        self.run_container(name, '--network', f'container:{self.name(letter, "host")}', '--user', 'postgres',
                           '--env-file', self.private/'patroni.env',
                           '-v', f'{self.name(letter, "tls")}:/etc/pickup-pact/tls:ro',
                           '-v', f'{self.config}/hosts/pact-{letter}/patroni.yml:/etc/pickup-pact/patroni.yml:ro',
                           '-v', f'{self.name(letter, "data")}:/var/lib/postgresql/17',
                           '-v', f'{self.name(letter, "spool")}:/var/lib/pickup-pact/wal-spool',
                           '-v', f'{ROOT}/infra/ha/pickup-wal-archive:/usr/local/bin/pickup-wal-archive:ro',
                           image=PATRONI_IMAGE, command=('/etc/pickup-pact/patroni.yml',))

    def members(self, via=None) -> list[dict]:
        for letter in ([via] if via else HOSTS):
            result = self.docker('exec', self.name(letter, 'patroni'), 'patronictl', '-c', '/etc/pickup-pact/patroni.yml',
                                 'list', '-f', 'json', check=False, timeout=30)
            if result.returncode == 0:
                return json.loads(result.stdout)
        raise RuntimeError('no reachable Patroni member could list the cluster')

    def leader(self, via=None) -> str | None:
        for member in self.members(via):
            if member.get('Role') == 'Leader' and member.get('State') == 'running':
                return member['Member'][-1]
        return None

    def wait_cluster(self, members=3, timeout=180, via=None, exclude=()):
        """Leader plus a streaming synchronous standby (and the other replica when expected)."""
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
            except RuntimeError:
                pass
            time.sleep(1)
        raise AssertionError('Patroni cluster did not converge: '+json.dumps(last))

    def psql_leader(self, sql: str, database='postgres', timeout=60, options=()):
        leader = self.leader()
        if leader is None:
            raise AssertionError('no Patroni leader')
        return self.docker('exec', '-i', '-u', 'postgres', self.name(leader, 'patroni'), 'psql', '-X', '-q',
                           *options, '-v', 'ON_ERROR_STOP=1', '-d', database, '-f', '-', stdin=sql, timeout=timeout)

    def bootstrap_roles(self):
        self.psql_leader((self.config/'cluster/bootstrap.sql').read_text())
        statements = []
        for role in ROLES:
            for kind, user in (('owner', inv.owner_user(role)), ('runtime', inv.runtime_user(role))):
                statements.append(f"ALTER ROLE {user} PASSWORD '{verifier(self.passwords[f'{role}_{kind}'])}';")
        self.psql_leader('\n'.join(statements)+'\n')
        for role in ROLES:
            passfile = self.private/f'{role}-owner.pgpass'
            passfile.write_text(f'*:*:*:{inv.owner_user(role)}:{self.passwords[role+"_owner"]}\n')
            os.chmod(passfile, 0o600)
            env = {**os.environ, 'PICKUP_MIGRATE_DSN': self.owner_dsn(role), 'PICKUP_HA_REHEARSAL': 'single_host_development',
                   'PYTHONPATH': f'{ROOT}:{ROOT}/services/reconciler'}
            result = subprocess.run([sys.executable, '-m', 'demo.route.ha.migrate', 'apply', '--role', role,
                                     '--runtime-user', inv.runtime_user(role)], cwd=ROOT, env=env,
                                    capture_output=True, text=True, timeout=120)
            if result.returncode:
                raise AssertionError('migration failed: '+self.redact(result.stderr)[-800:])
            self.timeline.append({'event': 'migrated', 'role': role, 'result': json.loads(result.stdout)})

    def owner_dsn(self, role):
        hosts, ports = inv.order_dsn_hosts(self.data)
        return (f'host={hosts} port={ports} dbname={inv.DATABASES[role]} user={inv.owner_user(role)} '
                f'sslmode=verify-full sslrootcert={self.ca.cert} passfile={self.private}/{role}-owner.pgpass '
                f'target_session_attrs=read-write connect_timeout=3')

    def runtime_dsn(self, role, *, member=None, attrs='read-write'):
        if member is None:
            hosts, ports = inv.order_dsn_hosts(self.data)
        else:
            hosts, ports = self.host(member).client_address, '5432'
        passfile = self.private/f'{role}-runtime.pgpass'
        if not passfile.exists():
            passfile.write_text(f'*:*:*:{inv.runtime_user(role)}:{self.passwords[role+"_runtime"]}\n')
            os.chmod(passfile, 0o600)
        return (f'host={hosts} port={ports} dbname={inv.DATABASES[role]} user={inv.runtime_user(role)} '
                f'sslmode=verify-full sslrootcert={self.ca.cert} passfile={passfile} '
                f'target_session_attrs={attrs} connect_timeout=2')

    def service_env(self, letter, role):
        return systemd_env(self.config/f'hosts/pact-{letter}/pickup-{role}.env')

    def start_service(self, letter, service):
        role, unit = SERVICES[service]
        name = self.name(letter, service)
        if name in self.created['container']:
            self.docker('start', name)
            return
        env = self.service_env(letter, role)
        env_file = self.private/f'{letter}-{service}.env'
        docker_env_file(env, env_file)
        self.run_container(name, '--network', f'container:{self.name(letter, "host")}', '--user', str(SERVICE_UID),
                           '--env-file', env_file, '--env-file', self.private/f'{role}-secrets.env',
                           '-e', 'HOME=/tmp', '-e', 'REPAIR_REVIEW_DB=/tmp/legacy-repair.sqlite',
                           '-v', f'{ROOT}:/opt/pickup-pact:ro', '-v', f'{self.name(letter, "tls")}:/etc/pickup-pact/tls:ro',
                           '-v', f'{self.name(letter, "secrets")}:/etc/pickup-pact/secrets:ro',
                           image=APP_IMAGE, command=unit_command(unit, env))

    # --------------------------------------------------------- HTTP harness
    def client(self, timeout=20):
        import httpx
        context = ssl.create_default_context(cafile=str(self.ca.cert))
        context.load_cert_chain(*self.harness_cert)
        return httpx.Client(verify=context, timeout=timeout, trust_env=False, follow_redirects=False)

    def app_url(self, letter):
        return f'https://{self.host(letter).address}:8000'

    def wait_apps(self, letters, timeout=120):
        deadline = time.monotonic()+timeout
        pending = set(letters)
        last = {}
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
                    except Exception as exc:  # noqa: BLE001 - readiness polling
                        last[letter] = type(exc).__name__
                time.sleep(.5)
        if pending:
            raise AssertionError('apps not ready: '+json.dumps(last))

    # ------------------------------------------------------- fault injection
    def kill_host(self, letter):
        """Abrupt loss of every process and the network namespace of one host (one SIGKILL call)."""
        names = [self.name(letter, part) for part in ('patroni', 'etcd', *START_ORDER, 'host')]
        self.docker('kill', *[name for name in names if self.owned('container', name)], check=False)

    def restart_service(self, letter, service):
        """Recreate one service container from the current rendered configuration."""
        name = self.name(letter, service)
        if not self.owned('container', name):
            raise RuntimeError('service container ownership not confirmed')
        logs = self.docker('logs', '--tail', '200', name, check=False)
        (self.evidence/f'{letter}-{service}-replaced.log').write_text(self.redact(logs.stdout+logs.stderr))
        self.docker('rm', '-f', '-v', name)
        self.created['container'].remove(name)
        self.start_service(letter, service)

    def migrate(self, *args):
        env = {**os.environ, 'PICKUP_HA_REHEARSAL': 'single_host_development',
               'PYTHONPATH': f'{ROOT}:{ROOT}/services/reconciler'}
        results = []
        for role in ROLES:
            result = subprocess.run([sys.executable, '-m', 'demo.route.ha.migrate', *args, '--role', role], cwd=ROOT,
                                    env=env | {'PICKUP_MIGRATE_DSN': self.owner_dsn(role)},
                                    capture_output=True, text=True, timeout=120)
            if result.returncode:
                raise AssertionError('owner command failed: '+self.redact(result.stderr)[-800:])
            results.append(json.loads(result.stdout))
        return results

    def rotate_tokens(self):
        """New internal role tokens and notification secret (post-restore key rotation)."""
        previous = dict(self.tokens)
        self.tokens = {name: secrets.token_hex(32) for name in previous}
        self.secrets += list(self.tokens.values())
        for role in ROLES:
            wanted = {'order': ['ROUTE_MERCHANT_TOKEN', 'ROUTE_PAYMENT_TOKEN', 'ROUTE_PAYMENT_NOTIFY_SECRET'],
                      'merchant': ['ROUTE_MERCHANT_TOKEN'],
                      'payment': ['ROUTE_PAYMENT_TOKEN', 'ROUTE_PAYMENT_NOTIFY_SECRET']}[role]
            docker_env_file({k: self.tokens[k] for k in wanted}, self.private/f'{role}-secrets.env')
        return previous

    def internal_transport(self, letter='b', generation=1):
        """The order role's mTLS identity from one host, for direct boundary probes."""
        from demo.route.ha.transport import TransportPolicy
        directory = self.private/f'tls-{letter}'
        return TransportPolicy.mtls(role='order', allowed_hosts=[h.address for h in inv.hosts_of(self.data)],
                                    ca_file=str(directory/'ca.crt'), cert_file=str(directory/'order.crt'),
                                    key_file=str(directory/'order.key'), generation=generation)

    def roll_generation(self, generation: int):
        """Render the inventory for a new operating generation and recreate every service."""
        self.data = {**self.data, 'operating': {'generation': generation}}
        target = self.root/f'config-generation-{generation}'
        inv.render(self.data, target)
        self.config = target
        for letter in HOSTS:
            for service in START_ORDER:
                self.restart_service(letter, service)

    def start_host(self, letter):
        self.start_namespace(letter)
        self.start_etcd(letter)
        self.start_patroni(letter)
        for service in START_ORDER:
            self.start_service(letter, service)

    def stop_part(self, letter, part, *, kill=True):
        self.docker('kill' if kill else 'stop', self.name(letter, part), check=False, timeout=60)

    def start_part(self, letter, part):
        self.docker('start', self.name(letter, part))

    def partition(self, letter):
        """Cut one host from the cluster network; its database stays reachable on the client network."""
        self.docker('network', 'disconnect', '--force', self.name('cluster'), self.name(letter, 'host'))

    def heal(self, letter):
        host = self.host(letter)
        self.docker('network', 'connect', '--ip', host.address, self.name('cluster'), self.name(letter, 'host'))

    # ----------------------------------------------------------------- close
    def collect_logs(self):
        logs = self.evidence/'logs'
        logs.mkdir(exist_ok=True)
        for name in self.created['container']:
            if self.owned('container', name):
                result = self.docker('logs', '--tail', '400', name, check=False, timeout=60)
                (logs/(name[len(self.prefix):]+'.log')).write_text(self.redact(result.stdout+result.stderr))

    def close(self):
        errors = []
        try:
            self.collect_logs()
        except Exception as exc:  # noqa: BLE001 - cleanup must continue
            errors.append('logs:'+type(exc).__name__)
        for kind in ('container', 'volume', 'network'):
            for name in reversed(self.created[kind]):
                if not self.owned(kind, name):
                    errors.append(f'{kind} ownership not confirmed; left in place')
                    continue
                args = {'container': ('rm', '-f', '-v'), 'volume': ('volume', 'rm'), 'network': ('network', 'rm')}[kind]
                if self.docker(*args, name, check=False, timeout=120).returncode:
                    errors.append(f'{kind} cleanup failed')
        shutil.rmtree(self.private, ignore_errors=True)
        if errors:
            raise RuntimeError('; '.join(errors))


def tempfile_dir() -> str:
    import tempfile
    return tempfile.gettempdir()


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--hold', type=int, default=0, help='seconds to keep the cluster for manual inspection')
    args = parser.parse_args()
    rehearsal = ClusterRehearsal(args.output)
    try:
        rehearsal.setup()
        print(json.dumps({'leader': rehearsal.leader(), 'members': rehearsal.members()}, indent=2))
        time.sleep(args.hold)
    finally:
        rehearsal.close()
