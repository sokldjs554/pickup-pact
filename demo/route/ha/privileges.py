"""Least-privilege contract for the ``ha_postgres_v1`` runtime database users.

Each data owner (order, merchant, payment) has an owner user that runs DDL and a
runtime user that the services use. The runtime user receives only the table
privileges below. Posted financial and merchant history is append-only for the
runtime user: it cannot UPDATE, DELETE or TRUNCATE it. A table that appears in
the schema without an explicit entry stops the migration instead of silently
inheriting access.

The operating generation is a single row per database. Restoring a backup and
re-opening service bumps it (owner user only), so processes configured for the
previous generation can no longer use the restored database. Idempotency and
operation keys are not regenerated.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path

ROLE_SCHEMAS = {'order': 'pact_orders', 'merchant': 'pact_merchants', 'payment': 'pact_payment'}
GENERATION_TABLE = 'pact_operating_generation'
GENERATION_HISTORY = 'pact_generation_history'
S, I, U, D = 'SELECT', 'INSERT', 'UPDATE', 'DELETE'

TABLE_PRIVILEGES = {
    'order': {
        'journeys': {S, I, U},
        'journey_requests': {S, I},
        'authorization_refs': {S, I},
        'notification_inbox': {S, I},
        'notification_hints': {S, I, U, D},
        'recovery_nodes': {S, I, U, D},
        'ha_operations': {S, I, U},
        GENERATION_TABLE: {S},
        GENERATION_HISTORY: set(),
    },
    'merchant': {
        'merchant_seats': {S, I, U},
        'merchant_policy': {S, I, U},
        'merchant_receipts': {S, I},
        'merchant_reply_loss': {S, I},
        GENERATION_TABLE: {S},
        GENERATION_HISTORY: set(),
    },
    'payment': {
        'pact_storage_meta': {S},
        'payment_settings': {S},
        'payment_authorizations': {S, I, U},
        'payment_commands': {S, I},
        'payment_transactions': {S, I},
        'payment_outbox': {S, I, U},
        'payment_faults': {S, I},
        GENERATION_TABLE: {S},
        GENERATION_HISTORY: set(),
    },
}
# Recorded history the runtime user may append to but never rewrite or remove.
APPEND_ONLY = {
    'order': ('journey_requests', 'notification_inbox', 'authorization_refs'),
    'merchant': ('merchant_receipts',),
    'payment': ('payment_transactions', 'payment_commands'),
}
DDL = {
    'order': ('operations.sql', 'orders.sql'),
    'merchant': ('merchants.sql',),
    'payment': ('payments.sql',),
}
GENERATION_DDL = f'''
CREATE TABLE IF NOT EXISTS {GENERATION_TABLE}(
    singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
    generation bigint NOT NULL CHECK(generation>=1),
    changed_at timestamptz NOT NULL DEFAULT clock_timestamp());
INSERT INTO {GENERATION_TABLE}(singleton,generation) VALUES(true,1) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS {GENERATION_HISTORY}(
    generation bigint PRIMARY KEY CHECK(generation>=2),
    previous bigint NOT NULL,
    reason text NOT NULL UNIQUE CHECK(reason ~ '^[A-Za-z0-9:_-]{{8,120}}$'),
    changed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    changed_by name NOT NULL DEFAULT current_user);
'''


def role(name: str) -> str:
    if name not in ROLE_SCHEMAS:
        raise ValueError('unknown data owner role')
    return name


def ddl(name: str) -> str:
    directory = Path(__file__).parent/'sql'
    return ''.join((directory/part).read_text() for part in DDL[role(name)]) + GENERATION_DDL


class PrivilegeViolation(ValueError):
    """The runtime database user holds more authority than the contract allows."""


class GenerationMismatch(OSError):
    """The database belongs to another operating generation; never write to it."""


@dataclass(frozen=True)
class RuntimeGuard:
    role: str
    generation: int

    def __post_init__(self):
        role(self.role)
        if type(self.generation) is not int or not 1 <= self.generation <= 1_000_000:
            raise ValueError('explicit operating generation required')

    def verify_privileges(self, db, schema: str) -> None:
        """Run once at pool start with the runtime user's own connection."""
        attributes = db.execute(
            '''SELECT rolsuper,rolcreaterole,rolcreatedb,rolbypassrls,rolreplication
               FROM pg_roles WHERE rolname=current_user''').fetchone()
        if attributes is None or any(attributes.values()):
            raise PrivilegeViolation('runtime database user has administrative attributes')
        scope = db.execute(
            '''SELECT has_database_privilege(current_user,current_database(),'CREATE') AS db_create,
                      has_schema_privilege(current_user,%s,'CREATE') AS schema_create,
                      (SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname=%s) AS schema_owner,
                      current_user::text AS me,
                      ARRAY(SELECT datname FROM pg_database
                            WHERE datallowconn AND NOT datistemplate AND datname<>current_database()
                              AND datname<>'postgres' AND has_database_privilege(current_user,datname,'CONNECT'))
                        AS other_databases''', (schema, schema)).fetchone()
        if scope['db_create'] or scope['schema_create'] or scope['schema_owner'] == scope['me']:
            raise PrivilegeViolation('runtime database user can change the schema')
        if scope['other_databases']:
            raise PrivilegeViolation("runtime database user can connect to another role's database")
        expected = TABLE_PRIVILEGES[self.role]
        rows = db.execute(
            '''SELECT c.relname AS name,
                      has_table_privilege(c.oid,'SELECT') AS s, has_table_privilege(c.oid,'INSERT') AS i,
                      has_table_privilege(c.oid,'UPDATE') AS u, has_table_privilege(c.oid,'DELETE') AS d,
                      has_table_privilege(c.oid,'TRUNCATE') AS t, has_table_privilege(c.oid,'REFERENCES') AS r,
                      has_table_privilege(c.oid,'TRIGGER') AS g
               FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
               WHERE n.nspname=%s AND c.relkind IN ('r','p','v','m','f')''', (schema,)).fetchall()
        seen = {row['name'] for row in rows}
        if seen != set(expected):
            raise PrivilegeViolation('schema tables differ from the reviewed privilege contract')
        for row in rows:
            held = {name for name, flag in ((S, row['s']), (I, row['i']), (U, row['u']), (D, row['d'])) if flag}
            if held != expected[row['name']] or row['t'] or row['r'] or row['g']:
                raise PrivilegeViolation('runtime table privileges differ from the reviewed contract')

    def check_generation(self, db, schema: str) -> None:
        from psycopg import sql
        row = db.execute(sql.SQL('SELECT generation FROM {}.{} WHERE singleton').format(
            sql.Identifier(schema), sql.Identifier(GENERATION_TABLE))).fetchone()
        if row is None or row['generation'] != self.generation:
            raise GenerationMismatch('database belongs to another operating generation')


def grant_statements(name: str, schema: str, runtime_user: str):
    """Owner-side GRANT/REVOKE statements, as composed SQL objects."""
    from psycopg import sql
    statements = [
        sql.SQL('REVOKE ALL ON SCHEMA {} FROM PUBLIC').format(sql.Identifier(schema)),
        sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(sql.Identifier(schema), sql.Identifier(runtime_user)),
        sql.SQL('REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM PUBLIC, {}').format(
            sql.Identifier(schema), sql.Identifier(runtime_user)),
        sql.SQL('REVOKE ALL ON ALL SEQUENCES IN SCHEMA {} FROM PUBLIC, {}').format(
            sql.Identifier(schema), sql.Identifier(runtime_user)),
    ]
    for table, privileges in sorted(TABLE_PRIVILEGES[role(name)].items()):
        if privileges:
            statements.append(sql.SQL('GRANT {} ON {}.{} TO {}').format(
                sql.SQL(', ').join(sql.SQL(p) for p in sorted(privileges)),
                sql.Identifier(schema), sql.Identifier(table), sql.Identifier(runtime_user)))
    return statements
