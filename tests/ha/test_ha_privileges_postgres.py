"""Real PostgreSQL: least-privilege runtime users and the operating generation.

The test creates its own labelled roles and databases in the explicitly opted-in
development cluster and removes only those. While it runs, PUBLIC CONNECT is
temporarily withdrawn from the other databases of that disposable cluster (and
restored exactly), because a production ha_postgres_v1 cluster has no database
that every user may enter.
"""
import os
import secrets
from uuid import uuid4

import pytest

from demo.route.ha.database import StorageUnavailable
from demo.route.ha.migrate import apply_on, bump_on
from demo.route.ha.privileges import GenerationMismatch, PrivilegeViolation, RuntimeGuard

ROLES = ('order', 'merchant', 'payment')


class Cluster(dict):
    def __repr__(self):
        return '<isolated test cluster; credentials withheld>'


@pytest.fixture(scope='module')
def cluster():
    dsn = os.environ.get('PICKUP_PG_TEST_DSN')
    if not dsn:
        pytest.skip('explicit isolated PostgreSQL required')
    assert os.environ.get('PICKUP_HA_TEST') == '1'
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    from psycopg.rows import dict_row
    run = 'pact_t' + uuid4().hex[:10]
    owner, owner_secret = run + '_owner', secrets.token_hex(24)
    users = {role: (run + '_' + role, secrets.token_hex(24)) for role in ROLES}
    databases = {role: run + '_' + role for role in ROLES}
    created_roles, created_dbs, withdrawn = [], [], []
    admin = psycopg.connect(dsn, autocommit=True)
    try:
        for name, secret in [(owner, owner_secret), *users.values()]:
            admin.execute(sql.SQL('CREATE ROLE {} LOGIN NOINHERIT PASSWORD {}').format(sql.Identifier(name), sql.Literal(secret)))
            admin.execute(sql.SQL('COMMENT ON ROLE {} IS {}').format(sql.Identifier(name), sql.Literal(run)))
            created_roles.append(name)
        for role, database in databases.items():
            admin.execute(sql.SQL('CREATE DATABASE {} OWNER {}').format(sql.Identifier(database), sql.Identifier(owner)))
            created_dbs.append(database)
            admin.execute(sql.SQL('COMMENT ON DATABASE {} IS {}').format(sql.Identifier(database), sql.Literal(run)))
            admin.execute(sql.SQL('REVOKE ALL ON DATABASE {} FROM PUBLIC').format(sql.Identifier(database)))
            admin.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(database),
                                                                              sql.Identifier(users[role][0])))
        for (name,) in admin.execute(
                "SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate AND datname<>'postgres' "
                "AND has_database_privilege('public',datname,'CONNECT')").fetchall():
            admin.execute(sql.SQL('REVOKE CONNECT ON DATABASE {} FROM PUBLIC').format(sql.Identifier(name)))
            withdrawn.append(name)
        owners = {role: make_conninfo(dsn, user=owner, password=owner_secret, dbname=databases[role]) for role in ROLES}
        runtime = {role: make_conninfo(dsn, user=users[role][0], password=users[role][1], dbname=databases[role])
                   for role in ROLES}
        for role in ROLES:
            with psycopg.connect(owners[role], row_factory=dict_row) as db:
                assert apply_on(db, role, users[role][0])['generation'] == 1
        yield Cluster(owners=owners, runtime=runtime, users=users, databases=databases, admin=admin, run=run)
    finally:
        for name in withdrawn:
            admin.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO PUBLIC').format(sql.Identifier(name)))
        for name in reversed(created_dbs):
            label = admin.execute("SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=%s",
                                  (name,)).fetchone()
            if label == (run,):
                admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
        for name in reversed(created_roles):
            label = admin.execute("SELECT shobj_description(oid,'pg_authid') FROM pg_roles WHERE rolname=%s",
                                  (name,)).fetchone()
            if label == (run,):
                admin.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(name)))
        admin.close()


def native_store(cluster, generation=1, worker='app-a'):
    from demo.route.ha.journey_store import PostgresJourneyStore
    from demo.route.ha.merchant_repository import PostgresMerchantFleet
    from demo.route.ha.payment_repository import PostgresPaymentRepository
    from native_helpers import RepositoryPaymentClient
    merchant = PostgresMerchantFleet(cluster['runtime']['merchant'], initialize=False,
                                     runtime_guard=RuntimeGuard('merchant', generation))
    payment = PostgresPaymentRepository(cluster['runtime']['payment'], initialize=False,
                                        runtime_guard=RuntimeGuard('payment', generation))
    store = PostgresJourneyStore(cluster['runtime']['order'], fleet=merchant, payment_gateway=RepositoryPaymentClient(payment),
                                 notification_secret='privilege-test-secret-32-characters', worker_id=worker,
                                 initialize=False, runtime_guard=RuntimeGuard('order', generation))
    return store, merchant, payment


def test_complete_journey_runs_with_only_the_reviewed_privileges(cluster):
    from test_native_journey import cmd, fresh, picked, quote
    from demo.route.reconciliation import reconcile
    store, merchant, payment = native_store(cluster)
    try:
        s = fresh(store)
        s = cmd(store, s, 'reserve', quote_id=quote(s, 'wave'))
        s = cmd(store, s, 'transfer', quote_id=quote(s, 'oat'))
        s = picked(store, s)
        proof = reconcile(store, s['id'])
        assert proof['status'] == 'MATCH' and proof['terminal']
        assert proof['payment']['captured_krw'] == 3200 and proof['payment']['capture_count'] == 1
        assert proof['payment']['held_krw'] == 0
        assert s['wallet']['spent'] == 1000 and s['wallet']['earned'] == 32
        assert store.repository.storage_ready() and merchant.storage_ready() and payment.storage_ready()
    finally:
        for item in (store, merchant, payment):
            item.close()


@pytest.mark.parametrize('role,table,statement', [
    ('payment', 'payment_transactions', 'UPDATE pact_payment.payment_transactions SET amount_krw=amount_krw'),
    ('payment', 'payment_transactions', 'DELETE FROM pact_payment.payment_transactions'),
    ('payment', 'payment_transactions', 'TRUNCATE pact_payment.payment_transactions'),
    ('payment', 'payment_commands', 'DELETE FROM pact_payment.payment_commands'),
    ('merchant', 'merchant_receipts', 'UPDATE pact_merchants.merchant_receipts SET order_id=order_id'),
    ('order', 'journey_requests', 'DELETE FROM pact_orders.journey_requests'),
    ('order', 'pact_operating_generation', 'UPDATE pact_orders.pact_operating_generation SET generation=99'),
    ('order', 'journeys', 'CREATE TABLE pact_orders.shadow(id int)'),
    ('order', 'journeys', 'ALTER TABLE pact_orders.journeys ADD COLUMN extra int'),
])
def test_runtime_users_cannot_rewrite_history_or_the_schema(cluster, role, table, statement):
    import psycopg
    with psycopg.connect(cluster['runtime'][role], autocommit=True) as db:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute(statement)


def test_runtime_user_cannot_enter_another_data_owners_database(cluster):
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    order = conninfo_to_dict(cluster['runtime']['order'])
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(make_conninfo(cluster['runtime']['order'], dbname=cluster['databases']['payment']),
                        connect_timeout=3).close()
    assert order['dbname'] == cluster['databases']['order']


def test_guard_refuses_excess_authority(cluster):
    from psycopg import sql
    from demo.route.ha.payment_repository import PostgresPaymentRepository
    admin, user = cluster['admin'], cluster['users']['payment'][0]
    other = sql.Identifier(cluster['databases']['order'])
    escalations = [
        ('GRANT DELETE ON TABLE pact_payment.payment_transactions TO {user}',
         'REVOKE DELETE ON TABLE pact_payment.payment_transactions FROM {user}', 'owner'),
        ('GRANT CONNECT ON DATABASE {other} TO {user}', 'REVOKE CONNECT ON DATABASE {other} FROM {user}', 'admin'),
        ('ALTER ROLE {user} CREATEDB', 'ALTER ROLE {user} NOCREATEDB', 'admin'),
        ('GRANT CREATE ON SCHEMA pact_payment TO {user}', 'REVOKE CREATE ON SCHEMA pact_payment FROM {user}', 'owner'),
    ]
    import psycopg
    for grant, revoke, by in escalations:
        target = admin if by == 'admin' else psycopg.connect(cluster['owners']['payment'], autocommit=True)
        target.execute(sql.SQL(grant).format(user=sql.Identifier(user), other=other))
        try:
            with pytest.raises(PrivilegeViolation):
                PostgresPaymentRepository(cluster['runtime']['payment'], initialize=False,
                                          runtime_guard=RuntimeGuard('payment', 1))
        finally:
            target.execute(sql.SQL(revoke).format(user=sql.Identifier(user), other=other))
            if target is not admin:
                target.close()
    # The schema owner is never accepted as a runtime user.
    with pytest.raises(PrivilegeViolation):
        PostgresPaymentRepository(cluster['owners']['payment'], initialize=False, runtime_guard=RuntimeGuard('payment', 1))
    with pytest.raises(ValueError):
        PostgresPaymentRepository(cluster['runtime']['payment'], initialize=True, runtime_guard=RuntimeGuard('payment', 1))
    repo = PostgresPaymentRepository(cluster['runtime']['payment'], initialize=False, runtime_guard=RuntimeGuard('payment', 1))
    repo.close()


def test_generation_bump_fences_running_processes_and_keeps_original_keys(cluster):
    import psycopg
    from psycopg.rows import dict_row
    from demo.route.ha.payment_repository import PostgresPaymentRepository
    old = PostgresPaymentRepository(cluster['runtime']['payment'], initialize=False,
                                    runtime_guard=RuntimeGuard('payment', 1))
    request = dict(action='AUTHORIZE', world_id='gen-world', order_id='PCT-gen', operation_key='gen-approve',
                   authorization_id='AUTH-gen', amount_krw=3200, currency='KRW', payment_revision=1,
                   quote_fingerprint='b'*64, card_token='demo-approved')
    try:
        before = old.execute(request)
        reason = 'restore-' + cluster['run']
        with psycopg.connect(cluster['owners']['payment'], row_factory=dict_row) as db:
            assert bump_on(db, 'payment', expected=1, reason=reason) == dict(role='payment', generation=2, changed=True)
            assert bump_on(db, 'payment', expected=1, reason=reason)['changed'] is False
            with pytest.raises(ValueError):
                bump_on(db, 'payment', expected=1, reason='another-restore-a')
            with pytest.raises(ValueError):
                bump_on(db, 'payment', expected=2, reason=reason)
        # The already-running pool checks the generation on every checkout.
        with pytest.raises(StorageUnavailable):
            old.operation('gen-approve')
        with pytest.raises(StorageUnavailable):
            old.execute(request | {'operation_key': 'gen-approve-retry'})
        with pytest.raises(GenerationMismatch):
            PostgresPaymentRepository(cluster['runtime']['payment'], initialize=False,
                                      runtime_guard=RuntimeGuard('payment', 1))
        current = PostgresPaymentRepository(cluster['runtime']['payment'], initialize=False,
                                            runtime_guard=RuntimeGuard('payment', 2))
        try:
            assert current.operation('gen-approve') == before
            assert current.execute(request) == before
            assert current.operation('gen-approve-retry') is None
            assert current.snapshot('gen-world', 'PCT-gen')['held_krw'] == 3200
        finally:
            current.close()
    finally:
        old.close()


def test_client_side_scram_verifier_logs_in_without_sending_the_password(cluster):
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    from demo.route.ha.scram import verifier
    admin, run = cluster['admin'], cluster['run']
    name, password = run + '_scram', secrets.token_hex(24)
    stored = verifier(password)
    assert password not in stored and stored.startswith('SCRAM-SHA-256$4096:')
    admin.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {}').format(sql.Identifier(name), sql.Literal(stored)))
    try:
        assert admin.execute('SELECT rolpassword FROM pg_authid WHERE rolname=%s', (name,)).fetchone()[0] == stored
        dsn = make_conninfo(cluster['runtime']['order'], user=name, password=password, dbname='postgres')
        with psycopg.connect(dsn) as db:
            assert db.execute('SELECT current_user').fetchone()[0] == name
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(make_conninfo(dsn, password=secrets.token_hex(24))).close()
    finally:
        admin.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(name)))
    for weak in ['short', 'ä'*30, 'x'*30+'\n']:
        with pytest.raises(ValueError):
            verifier(weak)
