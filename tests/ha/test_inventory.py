"""Deployment inventory gate: separation, quorum, backup isolation and approval.

Rendering is checked by feeding the rendered role settings back into the real
runtime validators, so the configuration cannot drift from what the services
accept. Nothing here contacts a host or creates a resource.
"""
import copy
import datetime as dt
import json
import os
from pathlib import Path
import re
import shlex

import pytest
import yaml

from demo.route.ha import inventory
from demo.route.ha.dsn import validate_ha_dsn
from demo.route.ha.settings import RoleSettings, ha_context
from tls_helpers import PrivateAuthority

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT/'infra/ha/inventory.example.yaml'
TODAY = dt.date(2026, 10, 2)


def example():
    return inventory.load(EXAMPLE)


def approved():
    data = example()
    for index, host in enumerate(data['hosts']):
        host['physical_host'] = f'vm-{index}'
    data['backup']['host']['physical_host'] = 'vm-backup'
    data['entry']['public_name'] = 'pickup.pact-rehearsal.net'
    data['approval'] = {'approved_by': 'operator', 'approved_on': '2026-10-01', 'monthly_cost_cap_krw': 90000,
                        'paid_resources': True, 'production_addresses': ['pickup-pact-demo.onrender.com'],
                        'destructive': {'test_id': 'ha-rehearsal-0a1b2c3d', 'hosts': ['pact-a', 'pact-b', 'pact-c']}}
    return data


def problems(data, stage='review'):
    return inventory.validate(data, stage, today=TODAY)


def test_example_is_reviewable_but_not_deployable_without_approval():
    assert problems(example()) == []
    deploy = problems(example(), 'deploy')
    assert 'approval.approved_by: named approver required' in deploy
    assert 'approval.approved_on: ISO date required' in deploy
    assert any(item.endswith('physical_host: placeholder value') for item in deploy)
    assert problems(approved(), 'deploy') == [] and problems(approved(), 'destructive') == []


def mutate(path, value):
    def apply(data):
        target = data
        for part in path[:-1]:
            target = target[part]
        if value is KeyError:
            del target[path[-1]]
        else:
            target[path[-1]] = value
        return data
    return apply


REVIEW_CASES = {
    'same failure domain': (mutate(['hosts', 1, 'failure_domain'], 'zone-a'), 'postgres: every member needs its own failure domain'),
    'same physical host': (mutate(['hosts', 2, 'physical_host'], 'CHANGE_ME-instance-a'), 'etcd: every member needs its own physical host'),
    'two etcd members': (mutate(['hosts', 2, 'roles'], ['postgres', 'order', 'merchant', 'payment']), 'etcd: an odd number of at least 3 hosts required'),
    'one payment host': (lambda d: [h['roles'].remove('payment') for h in d['hosts'][1:]] and d, 'payment: at least 2 hosts required'),
    'two hosts': (lambda d: d['hosts'].pop() and d, 'hosts: three to seven hosts are required'),
    'loopback address': (mutate(['hosts', 0, 'address'], '127.0.0.1'), 'pact-a: routable private address required'),
    'public address': (mutate(['hosts', 0, 'address'], '8.8.8.8'), 'pact-a: cluster members must use private addresses'),
    'duplicate dns': (mutate(['hosts', 1, 'dns'], 'pact-a.pact.internal'), 'hosts: duplicate dns'),
    'unknown role': (mutate(['hosts', 0, 'roles'], ['postgres', 'admin']), 'pact-a: roles must be distinct values'),
    'watchdog off': (mutate(['postgres', 'watchdog'], 'off'), 'postgres.watchdog: required (off only in a single-host rehearsal)'),
    'async replication': (mutate(['postgres', 'synchronous_node_count'], 0), 'postgres.synchronous_node_count'),
    'all standbys sync': (mutate(['postgres', 'synchronous_node_count'], 3), 'postgres.synchronous_node_count'),
    'unpinned image': (mutate(['postgres', 'image'], 'postgres:17'), 'postgres.image: image pinned by sha256 digest required'),
    'postgres 16': (mutate(['postgres', 'version'], '16.4'), 'postgres.version'),
    'address outside cidr': (mutate(['postgres', 'client_cidrs'], ['10.99.0.0/16']), 'pact-a: address outside postgres.client_cidrs'),
    'open cidr': (mutate(['postgres', 'client_cidrs'], ['0.0.0.0/0']), 'postgres.client_cidrs: bounded private networks only'),
    'one entry host': (mutate(['entry', 'hosts'], ['pact-a']), 'entry.hosts: at least two hosts carrying the entry role required'),
    'entry not on hosts': (mutate(['entry', 'hosts'], ['pact-a', 'pact-c']), 'entry.hosts'),
    'vip missing': (mutate(['entry', 'method'], 'keepalived_vip'), 'entry.virtual_ip'),
    'unknown entry': (mutate(['entry', 'method'], 'single_proxy'), 'entry.method'),
    'lb without forwarders': (lambda d: d['entry'].update(method='managed_lb', provider='x') or d, 'entry.forwarder_addresses'),
    'backup on db host': (mutate(['backup', 'host', 'dns'], 'pact-a.pact.internal'), 'backup.host: must not be a cluster host'),
    'backup same vm': (mutate(['backup', 'host', 'physical_host'], 'CHANGE_ME-instance-b'), 'backup.host: must not be a cluster host'),
    'backup in db zone': (mutate(['backup', 'host', 'failure_domain'], 'zone-b'), 'backup.host: needs a failure domain without database members'),
    'plain http backup': (mutate(['backup', 'repository_url'], 'http://backup.pact.internal:8000/pickup'), 'backup.repository_url'),
    'backup url elsewhere': (mutate(['backup', 'repository_url'], 'https://pact-a.pact.internal:8000/pickup'), 'backup.repository_url'),
    'writer can delete': (mutate(['backup', 'writer'], 'read_write'), 'backup.writer'),
    'maintainer on db host': (mutate(['backup', 'maintainer_credential_location'], 'pact-a'), 'backup.maintainer_credential_location'),
    'key only with data': (mutate(['backup', 'key_escrow'], ['backup-repository', 'pact-backup']), 'backup.key_escrow'),
    'key only on db host': (mutate(['backup', 'key_escrow'], ['pact-a', 'pact-b.pact.internal']), 'backup.key_escrow'),
    'receipts only on db': (mutate(['backup', 'receipt_locations'], ['pact-a']), 'backup.receipt_locations'),
    'one retained backup': (mutate(['backup', 'retain_base_backups'], 1), 'backup.retain_base_backups'),
    'slow wal archive': (mutate(['backup', 'max_wal_archive_delay_seconds'], 3600), 'backup.max_wal_archive_delay_seconds'),
    'stale alarm too early': (mutate(['backup', 'max_base_backup_age_hours'], 12), 'backup.max_base_backup_age_hours'),
    'no capacity': (mutate(['backup', 'capacity_gib'], KeyError), 'backup.capacity_gib'),
    'bad schema': (mutate(['schema'], 'v0'), 'schema'),
}


@pytest.mark.parametrize('case', sorted(REVIEW_CASES))
def test_review_rejects_unsafe_topologies(case):
    change, expected = REVIEW_CASES[case]
    found = problems(change(example()))
    assert any(item.startswith(expected) for item in found), (case, found)


DEPLOY_CASES = {
    'future approval': (mutate(['approval', 'approved_on'], '2026-12-01'), 'approval.approved_on: approval date is in the future'),
    'paid without cap': (mutate(['approval', 'monthly_cost_cap_krw'], 0), 'approval.monthly_cost_cap_krw: positive cap required'),
    'cap missing': (mutate(['approval', 'monthly_cost_cap_krw'], None), 'approval.monthly_cost_cap_krw'),
    'implicit paid flag': (mutate(['approval', 'paid_resources'], 'yes'), 'approval.paid_resources'),
    'unpaid managed lb': (lambda d: d['entry'].update(method='managed_lb', provider='cloud', forwarder_addresses=['10.20.1.5'])
                          or d['approval'].update(paid_resources=False) or d, 'approval.paid_resources: a managed load balancer'),
    'rehearsal deploy': (lambda d: d.update(kind='single_host_rehearsal') or d, 'kind: only independent_hosts can be deployed'),
}


@pytest.mark.parametrize('case', sorted(DEPLOY_CASES))
def test_deploy_requires_recorded_approval_and_cost_cap(case):
    change, expected = DEPLOY_CASES[case]
    found = problems(change(approved()), 'deploy')
    assert any(item.startswith(expected) for item in found), (case, found)


def test_destructive_scope_is_labelled_and_never_touches_production():
    for change, expected in [
        (mutate(['approval', 'destructive', 'test_id'], 'prod'), 'approval.destructive.test_id'),
        (mutate(['approval', 'destructive', 'hosts'], ['pact-z']), 'approval.destructive.hosts'),
        (mutate(['approval', 'destructive', 'hosts'], []), 'approval.destructive.hosts'),
        (mutate(['approval', 'production_addresses'], []), 'approval.production_addresses'),
        (mutate(['approval', 'production_addresses'], ['10.20.1.12']), 'pact-b: listed as a production address'),
        (mutate(['approval', 'production_addresses'], ['backup.pact.internal']), 'backup.host: production backup store'),
    ]:
        found = problems(change(approved()), 'destructive')
        assert any(item.startswith(expected) for item in found), (expected, found)
        assert problems(change(approved()), 'deploy') == []  # only the destructive gate checks the scope


def test_rehearsal_may_share_one_machine_but_is_labelled(tmp_path):
    data = example()
    data['kind'] = 'single_host_rehearsal'
    for host in data['hosts']:
        host['physical_host'] = 'dev-laptop'
        host['failure_domain'] = 'same'
    data['postgres']['watchdog'] = 'off'
    assert problems(data) == []
    manifest = inventory.render(data, tmp_path/'out')
    assert manifest['evidence_scope'] == 'single_host_rehearsal_not_host_ha'
    assert manifest['stages']['deploy'] is False
    patroni = yaml.safe_load((tmp_path/'out/hosts/pact-a/patroni.yml').read_text())
    assert patroni['watchdog']['mode'] == 'off'
    assert 'PICKUP_HA_REHEARSAL=single_host_development' in (tmp_path/'out/hosts/pact-a/pickup-order.env').read_text()


def parse_env(path):
    values = {}
    for line in Path(path).read_text().splitlines():
        if not line or line.startswith('#'):
            continue
        key, value = line.split('=', 1)
        values[key] = shlex.split(value)[0] if value.startswith("'") else value
    return values


@pytest.fixture(scope='module')
def rendered(tmp_path_factory):
    out = tmp_path_factory.mktemp('render')/'config'
    manifest = inventory.render(approved(), out)
    return out, manifest


def test_render_writes_strict_patroni_quorum_and_tls_only_access(rendered):
    out, manifest = rendered
    assert manifest['stages'] == {'review': True, 'deploy': True, 'destructive': True}
    assert manifest['evidence_scope'] == 'configuration_only_not_deployed'
    for name in ['pact-a', 'pact-b', 'pact-c']:
        config = yaml.safe_load((out/f'hosts/{name}/patroni.yml').read_text())
        dcs = config['bootstrap']['dcs']
        assert dcs['synchronous_mode'] is True and dcs['synchronous_mode_strict'] is True
        assert dcs['synchronous_node_count'] == 1 and dcs['failsafe_mode'] is False
        assert config['watchdog']['mode'] == 'required'
        assert config['etcd3']['protocol'] == 'https' and len(config['etcd3']['hosts']) == 3
        assert config['restapi']['verify_client'] == 'required'
        hba = config['postgresql']['pg_hba']
        assert not any(re.search(r'\b(trust|password|md5)\b', line) for line in hba)
        assert all(line.startswith(('local all postgres peer', 'hostssl')) for line in hba[:-4])
        assert hba[-4:] == ['hostnossl all all 0.0.0.0/0 reject', 'hostnossl all all ::/0 reject',
                            'host all all 0.0.0.0/0 reject', 'host all all ::/0 reject']
        assert config['postgresql']['parameters']['ssl'] == 'on'
        assert 'password' not in json.dumps(config['postgresql']['authentication'])
        etcd = parse_env(out/f'hosts/{name}/etcd.env')
        assert etcd['ETCD_CLIENT_CERT_AUTH'] == 'true' and etcd['ETCD_PEER_CLIENT_CERT_AUTH'] == 'true'
        assert etcd['ETCD_INITIAL_CLUSTER'].count('https://') == 3


def test_render_entry_points_and_manifest_without_secrets(rendered):
    out, manifest = rendered
    proxies = sorted(path.parent.name for path in out.glob('hosts/*/haproxy.cfg'))
    assert proxies == ['pact-a', 'pact-b']
    text = (out/'hosts/pact-a/haproxy.cfg').read_text()
    assert 'retry-on conn-failure\n' in text and 'empty-response' not in text
    servers = [line for line in text.splitlines() if line.strip().startswith('server ')]
    assert len(servers) == 3 and all('ssl verify required verifyhost' in line for line in servers)
    records = json.loads((out/'cluster/entry-dns.json').read_text())
    assert [r['value'] for r in records] == ['10.20.1.11', '10.20.1.12']
    import hashlib
    for relative, expected in manifest['files'].items():
        content = (out/relative).read_bytes()
        assert hashlib.sha256(content).hexdigest() == expected
        assert not re.search(rb'password\s*=|PRIVATE KEY|BEGIN CERT', content, re.I)
    sql = (out/'cluster/bootstrap.sql').read_text()
    assert not re.search(r"PASSWORD\s+'", sql, re.I) and 'SCRAM-SHA-256$' not in sql
    assert 'REVOKE ALL ON DATABASE pact_payment FROM PUBLIC;' in sql
    with pytest.raises(ValueError):
        inventory.render(approved(), out)
    with pytest.raises(inventory.InventoryError):
        inventory.render(mutate(['backup', 'writer'], 'read_write')(approved()), out.parent/'other')


def test_rendered_role_settings_are_accepted_by_the_runtime_validators(rendered, tmp_path):
    """Swap the deployment paths for disposable files, then use the real parsers."""
    out, _ = rendered
    ca = PrivateAuthority(tmp_path/'pki')
    tls, secrets = tmp_path/'tls', tmp_path/'secrets'
    tls.mkdir()
    secrets.mkdir()
    (tls/'ca.crt').write_bytes(ca.cert.read_bytes())
    for role in ['order', 'merchant', 'payment']:
        cert, key = ca.issue(role, role=role, dns=('pact-a.pact.internal',), ips=())
        (tls/f'{role}.crt').write_bytes(Path(cert).read_bytes())
        (tls/f'{role}.key').write_bytes(Path(key).read_bytes())
        os.chmod(tls/f'{role}.key', 0o600)
        (secrets/f'{role}.pgpass').write_text('*:*:*:*:not-a-secret\n')
        os.chmod(secrets/f'{role}.pgpass', 0o600)

    def local(env):
        return {k: v.replace(inventory.TLS_DIR, str(tls)).replace(inventory.SECRET_DIR, str(secrets))
                for k, v in env.items()}
    shared = {'ROUTE_MERCHANT_TOKEN': 'm'*40, 'ROUTE_PAYMENT_TOKEN': 'p'*40, 'ROUTE_PAYMENT_NOTIFY_SECRET': 'n'*40}
    for name in ['pact-a', 'pact-b', 'pact-c']:
        order = local(parse_env(out/f'hosts/{name}/pickup-order.env')) | shared
        settings = RoleSettings.from_env(order)
        assert settings.backend == 'ha_postgres_v1' and settings.ha.transport.generation == 1
        assert settings.merchant_url == f'https://{name}.pact.internal:8443'
        assert len(settings.merchant_failovers) == 2 and len(settings.payment_failovers) == 2
        assert order['PICKUP_ENTRY_ADDRESSES'] == '10.20.1.11,10.20.1.12'
        for role in ['merchant', 'payment']:
            env = local(parse_env(out/f'hosts/{name}/pickup-{role}.env')) | shared
            context = ha_context(env, role)
            assert validate_ha_dsn(env[f'PICKUP_{role.upper()}_DSN'])['user'] == f'pact_{role}_runtime'
            server = context.server(env, {'order'})
            assert server.advertise_host == f'{name}.pact.internal' and server.bind_address.startswith('10.20.1.')
        callbacks = json.loads(parse_env(out/f'hosts/{name}/pickup-payment.env')['PICKUP_NOTIFICATION_URLS'])
        assert len(callbacks) == 3 and callbacks[0].startswith(f'https://{name}.')
        transport = ha_context(local(parse_env(out/f'hosts/{name}/pickup-payment.env')) | shared, 'payment').transport
        assert all(transport.origin(url, callback=True) for url in callbacks)


def test_systemd_units_reference_rendered_files_and_keep_secrets_separate():
    units = sorted((ROOT/'infra/ha/systemd').glob('*.service'))
    assert {u.name for u in units} >= {'pickup-order-app.service', 'pickup-order-worker.service',
                                        'pickup-order-notification.service', 'pickup-merchant.service',
                                        'pickup-payment.service', 'patroni.service', 'etcd.service'}
    for unit in units:
        text = unit.read_text()
        assert not re.search(r'(?i)(token|password|secret)\s*=', text.replace('EnvironmentFile=', ''))
        if unit.name.startswith('pickup-'):
            assert 'EnvironmentFile=/etc/pickup-pact/secrets/' in text and 'User=pickup' in text
            assert 'NoNewPrivileges=true' in text
    app = (ROOT/'infra/ha/systemd/pickup-order-app.service').read_text()
    assert '--ssl-cert-reqs 2' in app and '--forwarded-allow-ips ${PICKUP_ENTRY_ADDRESSES}' in app
    patroni = (ROOT/'infra/ha/systemd/patroni.service').read_text()
    assert 'ExecStartPre=/usr/bin/test -c /dev/watchdog' in patroni and 'Restart=no' in patroni


def test_cli_reports_every_problem_and_exit_status(tmp_path, capsys):
    import importlib.util
    spec = importlib.util.spec_from_file_location('ha_inventory_cli', ROOT/'scripts/ha_inventory.py')
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    assert cli.main(['validate', str(EXAMPLE), '--stage', 'review']) == 0
    assert cli.main(['validate', str(EXAMPLE), '--stage', 'deploy']) == 1
    report = json.loads(capsys.readouterr().out.split('\n}\n')[1]+'\n}')
    assert report['ok'] is False and len(report['problems']) >= 3
    broken = tmp_path/'broken.yaml'
    data = copy.deepcopy(example())
    data['backup']['writer'] = 'read_write'
    broken.write_text(yaml.safe_dump(data))
    assert cli.main(['render', str(broken), '--out', str(tmp_path/'out')]) == 1
    assert not (tmp_path/'out').exists()


def test_restore_bootstrap_renders_only_on_the_designated_leader_and_needs_a_new_generation(tmp_path):
    data = approved()
    data['restore'] = {'leader': 'pact-b', 'base_dir': '/var/lib/pickup-pact/restore/base',
                       'wal_dir': '/var/lib/pickup-pact/restore/wal', 'target_name': 'pact_target_0a1b2c'}
    assert any(p.startswith('restore: a restored cluster must run a later operating generation') for p in problems(data))
    data['operating'] = {'generation': 2}
    assert problems(data) == []
    for change in [dict(leader='pact-z'), dict(base_dir='/tmp/base'), dict(wal_dir='/var/lib/pickup-pact/restore/../x'),
                   dict(target_name='latest'), dict(extra=1)]:
        bad = copy.deepcopy(data)
        bad['restore'].update(change)
        assert any(p.startswith('restore:') for p in problems(bad)), change
    manifest = inventory.render(data, tmp_path/'out')
    leader = yaml.safe_load((tmp_path/'out/hosts/pact-b/patroni.yml').read_text())['bootstrap']
    other = yaml.safe_load((tmp_path/'out/hosts/pact-a/patroni.yml').read_text())['bootstrap']
    assert leader['method'] == 'pickup_restore' and 'method' not in other
    recovery = leader['pickup_restore']['recovery_conf']
    assert recovery == {'restore_command': 'cp /var/lib/pickup-pact/restore/wal/%f %p',
                        'recovery_target_name': 'pact_target_0a1b2c', 'recovery_target_action': 'promote',
                        'recovery_target_timeline': 'current'}
    assert leader['dcs']['synchronous_mode_strict'] is True
    assert parse_env(tmp_path/'out/hosts/pact-a/pickup-order.env')['PICKUP_OPERATING_GENERATION'] == '2'
    assert manifest['stages']['review'] is True
