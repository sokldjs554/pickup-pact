"""ha_postgres_v1 configuration: refuse anything weaker than the reviewed deployment.

These are configuration contracts. They prove what the processes will refuse to
start with; they are not evidence that independent hosts were used.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

from demo.route.ha.dsn import validate_ha_dsn
from demo.route.ha.privileges import (APPEND_ONLY, GENERATION_HISTORY, GENERATION_TABLE, TABLE_PRIVILEGES,
                                      ddl, RuntimeGuard)
from demo.route.ha.settings import (HA, MULTI_HOST_SCOPE, REHEARSAL_SCOPE, RoleSettings, ha_context,
                                    initialize_flag)
from tls_helpers import PrivateAuthority, tls_env

HOSTS = ['app-a.pact.internal', 'app-b.pact.internal', 'merchant-a.pact.internal', 'merchant-b.pact.internal',
         'payment-a.pact.internal', 'payment-b.pact.internal']


@pytest.fixture(scope='module')
def files(tmp_path_factory):
    root = tmp_path_factory.mktemp('ha-settings')
    ca = PrivateAuthority(root/'pki')
    cert, key = ca.issue('order', role='order')
    passfile = root/'pgpass'
    passfile.write_text('*:*:*:pact_order_runtime:not-a-real-password\n')
    os.chmod(passfile, 0o600)
    loose = root/'loose-pgpass'
    loose.write_text(passfile.read_text())
    os.chmod(loose, 0o644)
    return dict(ca=ca, cert=cert, key=key, passfile=str(passfile), loose=str(loose), root=root)


def dsn(files, **change):
    parts = dict(host='db-a.pact.internal,db-b.pact.internal,db-c.pact.internal', port='5432',
                 dbname='pact_orders', user='pact_order_runtime', sslmode='verify-full',
                 sslrootcert=str(files['ca'].cert), passfile=files['passfile'],
                 target_session_attrs='read-write')
    parts.update(change)
    return ' '.join(f'{k}={v}' for k, v in parts.items() if v is not None)


def ha_env(files, **change):
    env = {'PICKUP_ROUTE_BACKEND': HA, 'PICKUP_ORDER_DSN': dsn(files), 'PICKUP_NODE_ID': 'app-a',
           'ROUTE_MERCHANT_URL': 'https://merchant-a.pact.internal:8443',
           'ROUTE_MERCHANT_FAILOVER_URLS': '["https://merchant-b.pact.internal:8443"]',
           'ROUTE_PAYMENT_URL': 'https://payment-a.pact.internal:8444',
           'ROUTE_PAYMENT_FAILOVER_URLS': '["https://payment-b.pact.internal:8444"]',
           'ROUTE_MERCHANT_TOKEN': 'm'*40, 'ROUTE_PAYMENT_TOKEN': 'p'*40, 'ROUTE_PAYMENT_NOTIFY_SECRET': 's'*40,
           **tls_env(files['ca'], files['cert'], files['key'], hosts=HOSTS, generation=3)}
    env.update(change)
    return {k: v for k, v in env.items() if v is not None}


def test_multi_host_dsn_with_verified_identity_and_private_credentials_is_accepted(files):
    parsed = validate_ha_dsn(dsn(files))
    assert parsed['hosts'] == ('db-a.pact.internal', 'db-b.pact.internal', 'db-c.pact.internal')
    assert parsed['user'] == 'pact_order_runtime' and not parsed['loopback']
    assert validate_ha_dsn(dsn(files, port='5432,5433,5434'))['ports'] == ('5432', '5433', '5434')
    assert validate_ha_dsn(dsn(files, host='127.0.0.1', port='5432'), allow_loopback=True, minimum_hosts=1)['loopback']


@pytest.mark.parametrize('change', [
    dict(password='leak-me-please'), dict(sslmode='require'), dict(sslmode='verify-ca'), dict(sslmode=None),
    dict(sslrootcert=None), dict(sslrootcert='system'), dict(sslrootcert='relative.crt'),
    dict(host='db-a.pact.internal'), dict(host='127.0.0.1,db-b.pact.internal'), dict(host='/var/run/postgresql,db-b'),
    dict(host='db-a.pact.internal,db-a.pact.internal'), dict(host='db_a,db-b'), dict(port='5432,5433'),
    dict(target_session_attrs='any'), dict(target_session_attrs='prefer-standby'), dict(user='postgres'),
    dict(user='replicator'), dict(dbname=None), dict(options='-c default_transaction_read_only=off'),
    dict(service='pact'), dict(hostaddr='10.0.0.5'), dict(passfile=None), dict(channel_binding='disable'),
    dict(ssl_min_protocol_version='TLSv1.1'), dict(sslcert='/tmp/only-cert.crt'),
    dict(connect_timeout='0'), dict(load_balance_hosts='other'),
])
def test_weaker_or_ambiguous_database_settings_are_refused_without_echoing_them(files, change):
    with pytest.raises(ValueError) as error:
        validate_ha_dsn(dsn(files, **change))
    assert 'leak-me-please' not in str(error.value) and files['passfile'] not in str(error.value)


def test_database_secret_files_must_be_private(files):
    with pytest.raises(ValueError):
        validate_ha_dsn(dsn(files, passfile=files['loose']))
    with pytest.raises(ValueError):
        validate_ha_dsn(dsn(files, passfile=str(files['root']/'missing')))
    cert, key = files['cert'], files['key']
    assert validate_ha_dsn(dsn(files, passfile=None, sslcert=cert, sslkey=key))['user'] == 'pact_order_runtime'
    loose_key = files['root']/'loose.key'
    loose_key.write_bytes(Path(key).read_bytes())
    os.chmod(loose_key, 0o640)
    with pytest.raises(ValueError):
        validate_ha_dsn(dsn(files, passfile=None, sslcert=cert, sslkey=str(loose_key)))


def test_ha_order_settings_bind_tls_generation_privileges_and_two_origins_per_role(files):
    settings = RoleSettings.from_env(ha_env(files))
    assert settings.backend == HA and settings.ha.scope == MULTI_HOST_SCOPE and not settings.ha.rehearsal
    assert settings.ha.transport.remote and settings.ha.transport.generation == 3
    assert settings.ha.guard == RuntimeGuard('order', 3)
    assert settings.merchant_url == 'https://merchant-a.pact.internal:8443'
    assert settings.payment_failovers == ('https://payment-b.pact.internal:8444',)
    text = repr(settings)
    for secret in ['m'*40, 'p'*40, 's'*40, 'pact_order_runtime', files['key']]:
        assert secret not in text


@pytest.mark.parametrize('change', [
    dict(ROUTE_MERCHANT_URL='http://merchant-a.pact.internal:8443'),
    dict(ROUTE_PAYMENT_URL='https://payment-c.pact.internal:8444'),
    dict(ROUTE_PAYMENT_FAILOVER_URLS='[]'), dict(ROUTE_MERCHANT_FAILOVER_URLS=None),
    dict(ROUTE_PAYMENT_FAILOVER_URLS='["https://payment-a.pact.internal:8444"]'),
    dict(ROUTE_PAYMENT_TOKEN='m'*40), dict(ROUTE_PAYMENT_NOTIFY_SECRET='p'*40), dict(ROUTE_MERCHANT_TOKEN='m'*28),
    dict(PICKUP_ORDER_INIT_SCHEMA='1'), dict(PICKUP_ORDER_INIT_SCHEMA='yes'), dict(PICKUP_OPERATING_GENERATION=None),
    dict(PICKUP_OPERATING_GENERATION='0'), dict(PICKUP_INTERNAL_TLS_KEY=None), dict(PICKUP_INTERNAL_TLS_CA=None),
    dict(PICKUP_HA_REHEARSAL='production'), dict(PICKUP_ROUTE_BACKEND='ha_postgres_v2'),
    dict(PICKUP_INTERNAL_HOSTS=json.dumps(HOSTS+['localhost'])),
])
def test_ha_order_settings_refuse_downgrades(files, change):
    with pytest.raises(ValueError):
        RoleSettings.from_env(ha_env(files, **change))


def test_ha_settings_refuse_unsafe_database_configuration(files):
    for value in [dsn(files, sslmode='require'), dsn(files, host='db-a.pact.internal'),
                  dsn(files, password='x'*40), dsn(files, host='127.0.0.1,127.0.0.2')]:
        with pytest.raises(ValueError):
            RoleSettings.from_env(ha_env(files, PICKUP_ORDER_DSN=value))


def test_loopback_rehearsal_is_explicit_and_labelled(files):
    local = ha_env(files, PICKUP_INTERNAL_HOSTS='["localhost"]', ROUTE_MERCHANT_URL='https://localhost:18443',
                   ROUTE_MERCHANT_FAILOVER_URLS='["https://localhost:18444"]',
                   ROUTE_PAYMENT_URL='https://localhost:18445', ROUTE_PAYMENT_FAILOVER_URLS='["https://localhost:18446"]',
                   PICKUP_ORDER_DSN=dsn(files, host='127.0.0.1', port='15432'))
    with pytest.raises(ValueError):
        RoleSettings.from_env(local)
    rehearsal = RoleSettings.from_env(local | {'PICKUP_HA_REHEARSAL': 'single_host_development'})
    assert rehearsal.ha.rehearsal and rehearsal.ha.scope == REHEARSAL_SCOPE
    assert 'not_host_ha' in rehearsal.ha.scope


def test_role_contexts_and_initialization_rules(files):
    env = ha_env(files)
    for role in ['order', 'merchant', 'payment']:
        assert ha_context(env, role).guard.role == role
    with pytest.raises(ValueError):
        ha_context(env | {'PICKUP_ROUTE_BACKEND': 'postgresql_development'}, 'order')
    context = ha_context(env, 'merchant')
    assert initialize_flag({}, 'X', context) is False
    with pytest.raises(ValueError):
        initialize_flag({'X': '1'}, 'X', context)
    assert initialize_flag({'X': '1'}, 'X', None) is True
    for bind, advertise in [('', 'merchant-a.pact.internal'), ('10.0.0.5', ''), ('10.0.0.5', 'evil.example')]:
        with pytest.raises(ValueError):
            context.server({'PICKUP_BIND_ADDRESS': bind, 'PICKUP_ADVERTISE_HOST': advertise}, {'order'})
    server = context.server({'PICKUP_BIND_ADDRESS': '10.0.0.5', 'PICKUP_ADVERTISE_HOST': 'merchant-a.pact.internal'},
                            {'order'})
    assert server.url(8443) == 'https://merchant-a.pact.internal:8443'


def test_development_mode_is_unchanged_by_the_ha_mode(files):
    dev = {'PICKUP_ORDER_DSN': 'dbname=test', 'PICKUP_NODE_ID': 'node-a',
           'ROUTE_MERCHANT_URL': 'http://127.0.0.1:19001', 'ROUTE_MERCHANT_TOKEN': 'm'*32,
           'ROUTE_PAYMENT_URL': 'http://127.0.0.1:19002', 'ROUTE_PAYMENT_TOKEN': 'm'*32,
           'ROUTE_PAYMENT_NOTIFY_SECRET': 's'*32}
    settings = RoleSettings.from_env(dev)
    assert settings.backend == 'postgresql_development' and settings.ha is None
    with pytest.raises(ValueError):
        RoleSettings.from_env(dev | {'ROUTE_PAYMENT_URL': 'https://payment-a.pact.internal:8444'})


def test_privilege_contract_covers_every_table_and_keeps_history_append_only():
    for role, tables in TABLE_PRIVILEGES.items():
        declared = set(re.findall(r'CREATE TABLE IF NOT EXISTS ([a-z_]+)', ddl(role)))
        assert declared == set(tables), role
        assert tables[GENERATION_TABLE] == {'SELECT'} and tables[GENERATION_HISTORY] == set()
        for table in APPEND_ONLY[role]:
            assert tables[table] <= {'SELECT', 'INSERT'}, (role, table)
        assert all(privileges <= {'SELECT', 'INSERT', 'UPDATE', 'DELETE'} for privileges in tables.values())
    assert TABLE_PRIVILEGES['payment']['payment_transactions'] == {'SELECT', 'INSERT'}


def test_native_api_import_never_creates_sqlite_fallback_in_ha_mode(tmp_path):
    env = {**os.environ, 'PICKUP_ROUTE_BACKEND': HA, 'ROUTE_DB': str(tmp_path/'must-not-exist.sqlite')}
    process = subprocess.run([sys.executable, '-c', 'import demo.route.api; assert demo.route.api.store is None'],
                             env=env, capture_output=True, text=True, timeout=20)
    assert process.returncode == 0, process.stderr
    assert list(tmp_path.iterdir()) == []
