"""Deployment inventory for ``ha_postgres_v1``: validate first, then render.

An inventory names the hosts, their failure domains and roles, the external
backup store and the human approval. Validation reports every problem at once
and has three stages:

* ``review``      structure, quorum, separation and backup isolation;
* ``deploy``      plus a recorded approval and cost cap;
* ``destructive`` plus a labelled destruction scope that excludes every
                  production address.

``single_host_rehearsal`` inventories may place every member on one physical
host and run without a hardware watchdog. Their output is labelled as
development evidence and can never pass the ``deploy`` stage.

Rendering writes configuration only (Patroni, etcd, HAProxy/keepalived, role
environment files, bootstrap SQL, backup policy) and a manifest of hashes. It
never contacts a host, creates a resource or embeds a secret: passwords, tokens
and private keys are referenced by path and supplied by the operator.
"""
from __future__ import annotations
from dataclasses import dataclass
import datetime as dt
import hashlib
import ipaddress
import json
from pathlib import Path
import re

SCHEMA = 'pickup-pact-ha-inventory/v1'
KINDS = {'independent_hosts', 'single_host_rehearsal'}
STAGES = ('review', 'deploy', 'destructive')
HOST_ROLES = {'postgres', 'etcd', 'order', 'merchant', 'payment', 'entry'}
SERVICE_ROLES = ('order', 'merchant', 'payment')
ENTRY_METHODS = {'dns_multi_a', 'keepalived_vip', 'managed_lb'}
NAME = re.compile(r'^[a-z][a-z0-9-]{0,30}$')
DNS = re.compile(r'^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$')
DIGEST = re.compile(r'^[a-z0-9./_:-]+@sha256:[0-9a-f]{64}$')
TEST_ID = re.compile(r'^ha-rehearsal-[0-9a-f]{8,32}$')
PLACEHOLDER = re.compile(r'CHANGE_ME|TODO|example\.invalid', re.I)
TLS_DIR = '/etc/pickup-pact/tls'
SECRET_DIR = '/etc/pickup-pact/secrets'
DATABASES = {'order': 'pact_orders', 'merchant': 'pact_merchants', 'payment': 'pact_payment'}
SCHEMAS = {'order': 'pact_orders', 'merchant': 'pact_merchants', 'payment': 'pact_payment'}
DEFAULT_PORTS = {'postgres': 5432, 'patroni': 8008, 'etcd_client': 2379, 'etcd_peer': 2380,
                 'order': 8000, 'merchant': 8443, 'payment': 8444, 'notification': 8445, 'entry': 443}


class InventoryError(ValueError):
    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__('; '.join(self.problems))


@dataclass(frozen=True)
class Host:
    name: str
    dns: str
    address: str
    physical_host: str
    failure_domain: str
    roles: frozenset
    client_address: str = ''  # optional second interface for application-to-database traffic

    @property
    def database_address(self) -> str:
        return self.client_address or self.dns


def load(path) -> dict:
    import yaml
    data = yaml.safe_load(Path(path).read_text())
    if not isinstance(data, dict):
        raise InventoryError(['inventory must be a mapping'])
    return data


def digest(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def _ip(value):
    try:
        return ipaddress.ip_address(str(value))
    except ValueError:
        return None


def _hosts(data, problems) -> list[Host]:
    hosts = []
    raw = data.get('hosts')
    if not isinstance(raw, list) or not 3 <= len(raw) <= 7:
        problems.append('hosts: three to seven hosts are required')
        return hosts
    for index, item in enumerate(raw):
        where = f'hosts[{index}]'
        if not isinstance(item, dict):
            problems.append(where+': mapping required')
            continue
        name, dns, address = item.get('name'), item.get('dns'), item.get('address')
        roles = item.get('roles')
        if not isinstance(name, str) or not NAME.fullmatch(name):
            problems.append(where+': invalid host name')
            continue
        if not isinstance(dns, str) or not DNS.fullmatch(dns) or dns.endswith('.localhost'):
            problems.append(name+': internal DNS name required')
        ip = _ip(address)
        if ip is None or ip.is_loopback or ip.is_unspecified or ip.is_multicast or ip.is_link_local:
            problems.append(name+': routable private address required')
        elif not ip.is_private:
            problems.append(name+': cluster members must use private addresses')
        for field in ('physical_host', 'failure_domain'):
            if not isinstance(item.get(field), str) or not item[field].strip():
                problems.append(f'{name}: {field} required')
        if not isinstance(roles, list) or not roles or not set(roles) <= HOST_ROLES or len(set(roles)) != len(roles):
            problems.append(name+': roles must be distinct values from '+', '.join(sorted(HOST_ROLES)))
            roles = []
        client = item.get('client_address', '')
        if client:
            client_ip = _ip(client)
            if client_ip is None or not client_ip.is_private or client_ip.is_loopback or client_ip == ip:
                problems.append(name+': client_address must be another private address')
        hosts.append(Host(name, str(dns), str(address), str(item.get('physical_host', '')),
                          str(item.get('failure_domain', '')), frozenset(roles), str(client or '')))
    for field in ('name', 'dns', 'address'):
        values = [getattr(host, field) for host in hosts]
        if len(set(values)) != len(values):
            problems.append(f'hosts: duplicate {field}')
    clients = [host.client_address for host in hosts if host.client_address]
    if len(set(clients)) != len(clients) or set(clients) & {host.address for host in hosts}:
        problems.append('hosts: duplicate client_address')
    return hosts


def _members(hosts, role):
    return [host for host in hosts if role in host.roles]


def _spread(hosts, role, minimum, independent, problems, *, odd=False):
    members = _members(hosts, role)
    if len(members) < minimum or (odd and len(members) % 2 == 0):
        problems.append(f'{role}: {"an odd number of at least" if odd else "at least"} {minimum} hosts required')
    if independent:
        if len({m.failure_domain for m in members}) != len(members):
            problems.append(f'{role}: every member needs its own failure domain')
        if len({m.physical_host for m in members}) != len(members):
            problems.append(f'{role}: every member needs its own physical host')
    return members


def validate(data: dict, stage: str = 'review', *, today: dt.date | None = None) -> list[str]:
    """Return every problem (empty when the inventory passes the stage)."""
    if stage not in STAGES:
        raise ValueError('unknown validation stage')
    problems: list[str] = []
    if data.get('schema') != SCHEMA:
        problems.append('schema: '+SCHEMA+' required')
    kind = data.get('kind')
    if kind not in KINDS:
        problems.append('kind: independent_hosts or single_host_rehearsal required')
    independent = kind == 'independent_hosts'
    if not isinstance(data.get('name'), str) or not NAME.fullmatch(data['name']):
        problems.append('name: short lowercase inventory name required')
    hosts = _hosts(data, problems)
    by_name = {host.name: host for host in hosts}
    database = _spread(hosts, 'postgres', 3, independent, problems, odd=True)
    _spread(hosts, 'etcd', 3, independent, problems, odd=True)
    for role in SERVICE_ROLES:
        _spread(hosts, role, 2, independent, problems)
    if independent and len({h.physical_host for h in hosts}) < 3:
        problems.append('hosts: at least three distinct physical hosts required')

    postgres = data.get('postgres') or {}
    if not isinstance(postgres, dict):
        postgres = {}
        problems.append('postgres: mapping required')
    if not re.fullmatch(r'17\.\d{1,2}', str(postgres.get('version', ''))):
        problems.append('postgres.version: a PostgreSQL 17 minor release required')
    if not isinstance(postgres.get('image'), str) or not DIGEST.fullmatch(postgres['image']):
        problems.append('postgres.image: image pinned by sha256 digest required')
    sync = postgres.get('synchronous_node_count')
    if type(sync) is not int or not 1 <= sync <= max(1, len(database)-1):
        problems.append('postgres.synchronous_node_count: between 1 and members-1 required')
    watchdog = postgres.get('watchdog')
    if watchdog != 'required' and not (watchdog == 'off' and kind == 'single_host_rehearsal'):
        problems.append('postgres.watchdog: required (off only in a single-host rehearsal)')
    networks = []
    for value in postgres.get('client_cidrs') or []:
        try:
            network = ipaddress.ip_network(str(value), strict=True)
        except ValueError:
            problems.append('postgres.client_cidrs: invalid network')
            continue
        if not network.is_private or network.prefixlen < (8 if network.version == 4 else 32):
            problems.append('postgres.client_cidrs: bounded private networks only')
        networks.append(network)
    if not networks:
        problems.append('postgres.client_cidrs: at least one private network required')
    for host in hosts:
        for value in filter(None, [host.address, host.client_address]):
            ip = _ip(value)
            if ip is not None and networks and not any(ip in network for network in networks):
                problems.append(host.name+': address outside postgres.client_cidrs')

    entry = data.get('entry') or {}
    method = entry.get('method')
    if method not in ENTRY_METHODS:
        problems.append('entry.method: '+', '.join(sorted(ENTRY_METHODS))+' required')
    entry_hosts = entry.get('hosts') or []
    if method in {'dns_multi_a', 'keepalived_vip'}:
        if set(entry_hosts) != {h.name for h in _members(hosts, 'entry')} or len(entry_hosts) < 2:
            problems.append('entry.hosts: at least two hosts carrying the entry role required')
        if independent and len({by_name[n].failure_domain for n in entry_hosts if n in by_name}) < 2:
            problems.append('entry: entry hosts need two failure domains')
    if method == 'keepalived_vip':
        vip = _ip(entry.get('virtual_ip'))
        if vip is None or not vip.is_private or any(vip == _ip(h.address) for h in hosts):
            problems.append('entry.virtual_ip: unused private address required for keepalived')
    if method == 'managed_lb':
        if not str(entry.get('provider', '')).strip():
            problems.append('entry.provider: managed load balancer provider required')
        forwarders = entry.get('forwarder_addresses') or []
        if not forwarders or any(_ip(value) is None for value in forwarders):
            problems.append('entry.forwarder_addresses: load balancer source addresses required')
    if not isinstance(entry.get('public_name'), str) or not DNS.fullmatch(entry['public_name']):
        problems.append('entry.public_name: public DNS name required')

    problems += _backup_problems(data.get('backup') or {}, hosts, database, independent)
    operating = data.get('operating') or {}
    if type(operating.get('generation', 1)) is not int or not 1 <= operating.get('generation', 1) <= 1_000_000:
        problems.append('operating.generation: positive integer required')

    if stage in {'deploy', 'destructive'}:
        problems += _approval_problems(data, kind, method, today or dt.date.today())
        problems += [f'{path}: placeholder value' for path in _placeholders(data)]
    if stage == 'destructive':
        problems += _destructive_problems(data, hosts)
    return problems


def _backup_problems(backup, hosts, database, independent) -> list[str]:
    problems = []
    if not isinstance(backup, dict) or not backup:
        return ['backup: external backup store required']
    store = backup.get('host') or {}
    cluster = {h.name for h in hosts} | {h.dns for h in hosts} | {h.address for h in hosts}
    if not isinstance(store, dict) or not store.get('name') or not store.get('dns'):
        problems.append('backup.host: name, dns, physical_host and failure_domain required')
        store = {}
    if {store.get('name'), store.get('dns'), store.get('address')} & cluster or \
            (independent and store.get('physical_host') in {h.physical_host for h in hosts}):
        problems.append('backup.host: must not be a cluster host')
    if not store.get('failure_domain') or \
            (independent and store.get('failure_domain') in {h.failure_domain for h in database}):
        problems.append('backup.host: needs a failure domain without database members')
    url = str(backup.get('repository_url', ''))
    match = re.fullmatch(r'https://([a-z0-9.-]+):(\d{1,5})/[A-Za-z0-9._/-]{1,120}', url)
    if not match or match.group(1) != store.get('dns'):
        problems.append('backup.repository_url: HTTPS URL on the backup host required')
    if backup.get('writer') != 'append_only':
        problems.append('backup.writer: database hosts may only append (no delete or prune)')
    maintainer = backup.get('maintainer_credential_location')
    if not isinstance(maintainer, str) or not maintainer.strip() or maintainer in cluster:
        problems.append('backup.maintainer_credential_location: must be held off the database hosts')
    escrow = backup.get('key_escrow') or []
    off_path = [place for place in escrow if isinstance(place, str) and place.strip()
                and place not in cluster and place not in {'backup-repository', store.get('name'), store.get('dns')}]
    if not off_path:
        problems.append('backup.key_escrow: at least one location outside the database hosts and the backup store')
    receipts = backup.get('receipt_locations') or []
    if not [place for place in receipts if isinstance(place, str) and place.strip() and place not in cluster]:
        problems.append('backup.receipt_locations: at least one location outside the database hosts')
    checks = [('base_backup_interval_hours', 1, 168), ('retain_base_backups', 2, 90),
              ('max_wal_archive_delay_seconds', 30, 900), ('capacity_gib', 1, 1_000_000),
              ('max_base_backup_age_hours', 1, 336)]
    for field, low, high in checks:
        value = backup.get(field)
        if type(value) is not int or not low <= value <= high:
            problems.append(f'backup.{field}: integer between {low} and {high} required')
    ratio = backup.get('alert_free_ratio')
    if type(ratio) not in {int, float} or not 0.05 <= ratio <= 0.5:
        problems.append('backup.alert_free_ratio: between 0.05 and 0.5 required')
    if type(backup.get('max_base_backup_age_hours')) is int and type(backup.get('base_backup_interval_hours')) is int \
            and backup['max_base_backup_age_hours'] < backup['base_backup_interval_hours']:
        problems.append('backup.max_base_backup_age_hours: must not be shorter than the backup interval')
    return problems


def _approval_problems(data, kind, method, today) -> list[str]:
    problems = []
    if kind != 'independent_hosts':
        problems.append('kind: only independent_hosts can be deployed as HA')
    approval = data.get('approval') or {}
    if not str(approval.get('approved_by', '')).strip():
        problems.append('approval.approved_by: named approver required')
    try:
        approved = dt.date.fromisoformat(str(approval.get('approved_on', '')))
        if approved > today:
            problems.append('approval.approved_on: approval date is in the future')
    except ValueError:
        problems.append('approval.approved_on: ISO date required')
    cap = approval.get('monthly_cost_cap_krw')
    if type(cap) is not int or cap < 0:
        problems.append('approval.monthly_cost_cap_krw: non-negative integer required')
    paid = approval.get('paid_resources')
    if type(paid) is not bool:
        problems.append('approval.paid_resources: explicit true/false required')
    elif paid and (type(cap) is not int or cap <= 0):
        problems.append('approval.monthly_cost_cap_krw: positive cap required for paid resources')
    elif not paid and method == 'managed_lb':
        problems.append('approval.paid_resources: a managed load balancer is a paid resource')
    return problems


def _destructive_problems(data, hosts) -> list[str]:
    problems = []
    approval = data.get('approval') or {}
    scope = approval.get('destructive') or {}
    production = approval.get('production_addresses') or []
    if not isinstance(production, list) or not production:
        problems.append('approval.production_addresses: list every address that must never be touched')
        production = []
    if not isinstance(scope.get('test_id'), str) or not TEST_ID.fullmatch(scope['test_id']):
        problems.append('approval.destructive.test_id: ha-rehearsal-<hex> label required')
    names = {h.name: h for h in hosts}
    targets = scope.get('hosts') or []
    if not targets or any(name not in names for name in targets):
        problems.append('approval.destructive.hosts: subset of inventory hosts required')
    protected = {str(value).lower() for value in production}
    for host in hosts:
        if {host.name, host.dns, host.address} & protected:
            problems.append(host.name+': listed as a production address; destructive tests refused')
    backup = data.get('backup') or {}
    store = backup.get('host') or {}
    if {str(store.get('dns', '')).lower(), str(store.get('address', ''))} & protected:
        problems.append('backup.host: production backup store cannot be a rehearsal target')
    return problems


def _placeholders(data, path='') -> list[str]:
    found = []
    if isinstance(data, dict):
        for key, value in data.items():
            found += _placeholders(value, f'{path}.{key}' if path else str(key))
    elif isinstance(data, list):
        for index, value in enumerate(data):
            found += _placeholders(value, f'{path}[{index}]')
    elif isinstance(data, str) and PLACEHOLDER.search(data):
        found.append(path)
    return found


def require(data: dict, stage: str) -> None:
    problems = validate(data, stage)
    if problems:
        raise InventoryError(problems)


# ---------------------------------------------------------------- rendering

def hosts_of(data) -> list[Host]:
    problems = []
    hosts = _hosts(data, problems)
    if problems:
        raise InventoryError(problems)
    return hosts


def _ports(data):
    return {**DEFAULT_PORTS, **(data.get('ports') or {})}


def runtime_user(role: str) -> str:
    return f'pact_{role}_runtime'


def owner_user(role: str) -> str:
    return f'pact_{role}_owner'


def order_dsn_hosts(data):
    database = _members(hosts_of(data), 'postgres')
    ports = _ports(data)
    return ','.join(h.database_address for h in database), ','.join(str(ports['postgres']) for _ in database)


def role_dsn(data, role: str) -> str:
    hosts, ports = order_dsn_hosts(data)
    return ' '.join([f'host={hosts}', f'port={ports}', f'dbname={DATABASES[role]}', f'user={runtime_user(role)}',
                     'sslmode=verify-full', f'sslrootcert={TLS_DIR}/ca.crt', f'passfile={SECRET_DIR}/{role}.pgpass',
                     'target_session_attrs=read-write', 'connect_timeout=3', 'channel_binding=require'])


def entry_forwarders(data) -> list[str]:
    entry = data['entry']
    if entry['method'] == 'managed_lb':
        return [str(value) for value in entry['forwarder_addresses']]
    return [h.address for h in hosts_of(data) if h.name in entry['hosts']]


def _local_first(host, members):
    """Prefer the same host (no extra network hop), then the rest in a stable order."""
    return sorted(members, key=lambda member: (member.name != host.name, member.name))


def role_environment(data, host: Host, role: str) -> dict:
    hosts = hosts_of(data)
    ports = _ports(data)
    internal = sorted({h.dns for h in hosts if h.roles & set(SERVICE_ROLES)})
    env = {
        'PICKUP_ROUTE_BACKEND': 'ha_postgres_v1',
        'PICKUP_OPERATING_GENERATION': str(int((data.get('operating') or {}).get('generation', 1))),
        'PICKUP_INTERNAL_HOSTS': json.dumps(internal),
        'PICKUP_INTERNAL_TLS_CA': f'{TLS_DIR}/ca.crt',
        'PICKUP_INTERNAL_TLS_CERT': f'{TLS_DIR}/{role}.crt',
        'PICKUP_INTERNAL_TLS_KEY': f'{TLS_DIR}/{role}.key',
        'PICKUP_BIND_ADDRESS': host.address,
        'PICKUP_ADVERTISE_HOST': host.dns,
        f'PICKUP_{role.upper()}_PORT': str(ports[role]),
        f'PICKUP_{role.upper()}_DSN': role_dsn(data, role),
    }
    if data.get('kind') == 'single_host_rehearsal':
        env['PICKUP_HA_REHEARSAL'] = 'single_host_development'
    if role == 'order':
        merchants = _local_first(host, _members(hosts, 'merchant'))[:3]
        payments = _local_first(host, _members(hosts, 'payment'))[:3]
        env.update({
            'PICKUP_NODE_ID': host.name+'-order',
            'PICKUP_NOTIFICATION_PORT': str(ports['notification']),
            # Only the entry tier may set X-Forwarded-* headers.
            'PICKUP_ENTRY_ADDRESSES': ','.join(entry_forwarders(data)),
            'ROUTE_MERCHANT_URL': f'https://{merchants[0].dns}:{ports["merchant"]}',
            'ROUTE_MERCHANT_FAILOVER_URLS': json.dumps([f'https://{m.dns}:{ports["merchant"]}' for m in merchants[1:]]),
            'ROUTE_PAYMENT_URL': f'https://{payments[0].dns}:{ports["payment"]}',
            'ROUTE_PAYMENT_FAILOVER_URLS': json.dumps([f'https://{p.dns}:{ports["payment"]}' for p in payments[1:]]),
        })
    if role == 'payment':
        receivers = _local_first(host, _members(hosts, 'order'))[:3]
        env['PICKUP_NOTIFICATION_URLS'] = json.dumps(
            [f'https://{r.dns}:{ports["notification"]}/internal/payments/events' for r in receivers])
    return env


def env_line(key: str, value: str) -> str:
    """systemd EnvironmentFile syntax; single quotes keep JSON and DSN text literal."""
    if "'" in value or '\n' in value:
        raise ValueError('unsupported character in rendered environment value')
    return f"{key}='{value}'" if any(c in value for c in ' "#\\$;') else f'{key}={value}'


def _env_file(env: dict, secret_file: str) -> str:
    lines = ['# Rendered by scripts/ha_inventory.py. No secrets: tokens and passwords are read from',
             f'# {secret_file} (0600, operator supplied).']
    lines += [env_line(key, value) for key, value in sorted(env.items())]
    return '\n'.join(lines)+'\n'


def patroni_config(data, host: Host) -> dict:
    hosts = hosts_of(data)
    ports = _ports(data)
    postgres = data['postgres']
    etcd = _members(hosts, 'etcd')
    networks = [str(ipaddress.ip_network(c)) for c in postgres['client_cidrs']]
    hba = ['local all postgres peer']
    for network in networks:
        hba += [f'hostssl replication replicator {network} scram-sha-256',
                f'hostssl postgres rewind_user {network} scram-sha-256']
        for role, database in DATABASES.items():
            hba.append(f'hostssl {database} {owner_user(role)},{runtime_user(role)} {network} scram-sha-256')
    hba += ['hostnossl all all 0.0.0.0/0 reject', 'hostnossl all all ::/0 reject',
            'host all all 0.0.0.0/0 reject', 'host all all ::/0 reject']
    rehearsal = data.get('kind') == 'single_host_rehearsal'
    tls = {'certfile': f'{TLS_DIR}/patroni.crt', 'keyfile': f'{TLS_DIR}/patroni.key', 'cafile': f'{TLS_DIR}/ca.crt'}
    return {
        'scope': data['name'], 'namespace': '/pickup-pact/', 'name': host.name,
        'restapi': {'listen': f'{host.address}:{ports["patroni"]}',
                    'connect_address': f'{host.dns}:{ports["patroni"]}', **tls, 'verify_client': 'required'},
        'ctl': {'insecure': False, 'certfile': tls['certfile'], 'keyfile': tls['keyfile'], 'cacert': tls['cafile']},
        'etcd3': {'hosts': [f'{m.dns}:{ports["etcd_client"]}' for m in etcd], 'protocol': 'https',
                  'cacert': tls['cafile'], 'cert': f'{TLS_DIR}/etcd-client.crt', 'key': f'{TLS_DIR}/etcd-client.key'},
        'bootstrap': {
            'dcs': {'ttl': 30, 'loop_wait': 10, 'retry_timeout': 10, 'maximum_lag_on_failover': 1048576,
                    'synchronous_mode': True, 'synchronous_mode_strict': True,
                    'synchronous_node_count': postgres['synchronous_node_count'], 'failsafe_mode': False,
                    'postgresql': {'use_pg_rewind': True, 'use_slots': True, 'parameters': {
                        'wal_level': 'replica', 'hot_standby': 'on', 'max_wal_senders': 10,
                        'max_replication_slots': 10, 'wal_log_hints': 'on', 'archive_mode': 'on',
                        'archive_timeout': '60s', 'synchronous_commit': 'on', 'max_connections': 200}}},
            'initdb': [{'encoding': 'UTF8'}, 'data-checksums', {'locale': 'C.UTF-8'}],
        },
        'postgresql': {
            'listen': ','.join(filter(None, [host.address, host.client_address]))+f':{ports["postgres"]}',
            'connect_address': f'{host.dns}:{ports["postgres"]}',
            'data_dir': '/var/lib/postgresql/17/pickup', 'bin_dir': '/usr/lib/postgresql/17/bin',
            'pgpass': '/var/lib/postgresql/.patroni.pgpass',
            # Patroni's own superuser session stays on the local socket (peer); no remote superuser login.
            'use_unix_socket': True, 'use_unix_socket_repl': False,
            # Passwords come from PATRONI_SUPERUSER_PASSWORD / _REPLICATION_ / _REWIND_ in a 0600 unit file.
            'authentication': {
                'superuser': {'username': 'postgres', 'sslmode': 'verify-full', 'sslrootcert': tls['cafile']},
                'replication': {'username': 'replicator', 'sslmode': 'verify-full', 'sslrootcert': tls['cafile']},
                'rewind': {'username': 'rewind_user', 'sslmode': 'verify-full', 'sslrootcert': tls['cafile']}},
            'parameters': {'ssl': 'on', 'ssl_cert_file': f'{TLS_DIR}/postgres.crt',
                           'ssl_key_file': f'{TLS_DIR}/postgres.key', 'ssl_ca_file': tls['cafile'],
                           'ssl_min_protocol_version': 'TLSv1.2', 'password_encryption': 'scram-sha-256',
                           'archive_command': '/usr/local/bin/pickup-wal-archive "%p" "%f"'},
            'pg_hba': hba,
        },
        'watchdog': {'mode': 'off' if rehearsal and postgres['watchdog'] == 'off' else 'required',
                     'device': '/dev/watchdog', 'safety_margin': 5},
        'tags': {'nofailover': False, 'noloadbalance': False, 'clonefrom': False, 'nosync': False,
                 'failure_domain': host.failure_domain},
    }


def etcd_environment(data, host: Host) -> dict:
    hosts = hosts_of(data)
    ports = _ports(data)
    members = _members(hosts, 'etcd')
    return {
        'ETCD_NAME': host.name, 'ETCD_DATA_DIR': '/var/lib/etcd/pickup-pact',
        'ETCD_LISTEN_PEER_URLS': f'https://{host.address}:{ports["etcd_peer"]}',
        'ETCD_LISTEN_CLIENT_URLS': f'https://{host.address}:{ports["etcd_client"]}',
        'ETCD_INITIAL_ADVERTISE_PEER_URLS': f'https://{host.dns}:{ports["etcd_peer"]}',
        'ETCD_ADVERTISE_CLIENT_URLS': f'https://{host.dns}:{ports["etcd_client"]}',
        'ETCD_INITIAL_CLUSTER': ','.join(f'{m.name}=https://{m.dns}:{ports["etcd_peer"]}' for m in members),
        'ETCD_INITIAL_CLUSTER_STATE': 'new',
        'ETCD_INITIAL_CLUSTER_TOKEN': data['name']+'-'+digest(data)[:12],
        'ETCD_CLIENT_CERT_AUTH': 'true', 'ETCD_TRUSTED_CA_FILE': f'{TLS_DIR}/ca.crt',
        'ETCD_CERT_FILE': f'{TLS_DIR}/etcd.crt', 'ETCD_KEY_FILE': f'{TLS_DIR}/etcd.key',
        'ETCD_PEER_CLIENT_CERT_AUTH': 'true', 'ETCD_PEER_TRUSTED_CA_FILE': f'{TLS_DIR}/ca.crt',
        'ETCD_PEER_CERT_FILE': f'{TLS_DIR}/etcd.crt', 'ETCD_PEER_KEY_FILE': f'{TLS_DIR}/etcd.key',
        'ETCD_AUTO_COMPACTION_RETENTION': '1', 'ETCD_QUOTA_BACKEND_BYTES': str(2*1024**3),
    }


def haproxy_config(data, host: Host) -> str:
    hosts = hosts_of(data)
    ports = _ports(data)
    lines = [
        '# Rendered entry point. Public TLS terminates here; the app hop is TLS with a client certificate.',
        'global', '    log stdout format raw local0', '    maxconn 2000',
        '    ssl-default-bind-options ssl-min-ver TLSv1.2', '    ssl-default-server-options ssl-min-ver TLSv1.2',
        'defaults', '    mode http', '    timeout connect 3s', '    timeout client 30s', '    timeout server 30s',
        '    option httplog', '    retries 1', '    option redispatch',
        'frontend public',
        f'    bind {host.address}:{ports["entry"]} ssl crt /etc/pickup-pact/tls/public.pem',
        '    option forwardfor', '    http-request set-header X-Forwarded-Proto https',
        '    default_backend order_apps',
        'backend order_apps', '    balance roundrobin',
        '    option httpchk GET /ready', '    http-check expect status 200',
        # Retry only when the request never reached a server. A request whose reply was lost is not
        # replayed by the proxy; the client repeats it with the same request ID.
        '    retry-on conn-failure',
    ]
    for member in _local_first(host, _members(hosts, 'order')):
        lines.append(f'    server {member.name} {member.dns}:{ports["order"]} check inter 2s fall 2 rise 2 '
                     f'ssl verify required verifyhost {member.dns} ca-file {TLS_DIR}/ca.crt '
                     f'crt {TLS_DIR}/entry.pem sni str({member.dns})')
    return '\n'.join(lines)+'\n'


def keepalived_config(data, host: Host) -> str:
    entry = data['entry']
    members = [m for m in hosts_of(data) if m.name in entry['hosts']]
    priority = 150 - 10*[m.name for m in sorted(members, key=lambda m: m.name)].index(host.name)
    return '\n'.join([
        '# VRRP needs one shared L2 segment; it does not span regions. Password comes from an include file.',
        'vrrp_script haproxy_alive {', '    script "/usr/bin/pgrep -x haproxy"', '    interval 2', '    fall 2',
        '    rise 2', '}', 'vrrp_instance pickup_entry {', '    state BACKUP', '    nopreempt',
        '    interface ' + str(entry.get('interface', 'eth0')), '    virtual_router_id ' + str(entry.get('vrid', 51)),
        f'    priority {priority}', '    advert_int 1', f'    unicast_src_ip {host.address}', '    unicast_peer {',
        *[f'        {m.address}' for m in members if m.name != host.name], '    }',
        f'    include {SECRET_DIR}/keepalived-auth.conf',
        f'    virtual_ipaddress {{ {entry["virtual_ip"]} }}', '    track_script { haproxy_alive }', '}', ''])


def bootstrap_sql(data) -> str:
    lines = ['-- Run once on the Patroni leader as the superuser, then set passwords from the secret',
             '-- store with \\password or a SCRAM verifier. This file contains no secrets.',
             'REVOKE ALL ON DATABASE postgres FROM PUBLIC;',
             'REVOKE CREATE ON SCHEMA public FROM PUBLIC;']
    for role, database in DATABASES.items():
        owner, runtime = owner_user(role), runtime_user(role)
        lines += [
            f'CREATE ROLE {owner} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;',
            f'CREATE ROLE {runtime} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;',
            f'CREATE DATABASE {database} OWNER {owner};',
            f'REVOKE ALL ON DATABASE {database} FROM PUBLIC;',
            f'GRANT CONNECT ON DATABASE {database} TO {owner}, {runtime};',
        ]
    lines += ['-- Patroni creates replicator and rewind_user (with the pg_rewind function grants) at bootstrap.',
              'GRANT CONNECT ON DATABASE postgres TO rewind_user;',
              '-- Next: python -m demo.route.ha.migrate apply --role <order|merchant|payment> '
              '--runtime-user pact_<role>_runtime (as pact_<role>_owner).']
    return '\n'.join(lines)+'\n'


def backup_policy(data) -> dict:
    backup = data['backup']
    return {
        'repository_url': backup['repository_url'],
        'writer': 'append_only',
        'maintainer_credential_location': backup['maintainer_credential_location'],
        'key_escrow': backup['key_escrow'], 'receipt_locations': backup['receipt_locations'],
        'base_backup_interval_hours': backup['base_backup_interval_hours'],
        'max_base_backup_age_hours': backup['max_base_backup_age_hours'],
        'retain_base_backups': backup['retain_base_backups'],
        'max_wal_archive_delay_seconds': backup['max_wal_archive_delay_seconds'],
        'capacity_gib': backup['capacity_gib'], 'alert_free_ratio': backup['alert_free_ratio'],
    }


def render(data: dict, out: Path) -> dict:
    """Write the reviewed configuration into an empty directory and return the manifest."""
    require(data, 'review')
    out = Path(out)
    if out.exists() and any(out.iterdir()):
        raise ValueError('render target must be empty')
    out.mkdir(parents=True, exist_ok=True)
    import yaml
    files = {}

    def write(relative, text):
        path = out/relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        files[relative] = hashlib.sha256(text.encode()).hexdigest()

    for host in hosts_of(data):
        base = f'hosts/{host.name}/'
        if 'postgres' in host.roles:
            write(base+'patroni.yml', yaml.safe_dump(patroni_config(data, host), sort_keys=False))
        if 'etcd' in host.roles:
            write(base+'etcd.env', ''.join(f'{k}={v}\n' for k, v in etcd_environment(data, host).items()))
        for role in SERVICE_ROLES:
            if role in host.roles:
                write(base+f'pickup-{role}.env', _env_file(role_environment(data, host, role), f'{SECRET_DIR}/{role}.env'))
        if 'entry' in host.roles and data['entry']['method'] in {'dns_multi_a', 'keepalived_vip'}:
            write(base+'haproxy.cfg', haproxy_config(data, host))
            if data['entry']['method'] == 'keepalived_vip':
                write(base+'keepalived.conf', keepalived_config(data, host))
    write('cluster/bootstrap.sql', bootstrap_sql(data))
    write('cluster/backup-policy.json', json.dumps(backup_policy(data), indent=2, sort_keys=True)+'\n')
    if data['entry']['method'] == 'dns_multi_a':
        records = [{'name': data['entry']['public_name'], 'type': 'A', 'value': h.address, 'health_check': '/ready'}
                   for h in hosts_of(data) if h.name in data['entry']['hosts']]
        write('cluster/entry-dns.json', json.dumps(records, indent=2)+'\n')
    manifest = {
        'schema': 'pickup-pact-ha-render/v1', 'inventory': data['name'], 'inventory_sha256': digest(data),
        'kind': data['kind'],
        'evidence_scope': ('single_host_rehearsal_not_host_ha' if data['kind'] == 'single_host_rehearsal'
                           else 'configuration_only_not_deployed'),
        'stages': {stage: not validate(data, stage) for stage in STAGES},
        'files': dict(sorted(files.items())),
    }
    (out/'MANIFEST.json').write_text(json.dumps(manifest, indent=2, sort_keys=True)+'\n')
    return manifest
