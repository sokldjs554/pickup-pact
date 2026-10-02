"""Owner-side schema and generation commands for ``ha_postgres_v1``.

Services never run DDL in this mode. The owner user applies the reviewed schema,
grants the runtime user exactly the contract in ``privileges.py`` and, after a
restore, advances the operating generation with a compare-and-set. The command
reads the owner DSN from ``PICKUP_MIGRATE_DSN`` and never prints it.

    python -m demo.route.ha.migrate apply --role payment --runtime-user pact_payment_runtime
    python -m demo.route.ha.migrate generation --role payment
    python -m demo.route.ha.migrate bump-generation --role payment --expected 1 --reason restore-20261002-a
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sys

from .dsn import validate_ha_dsn
from .privileges import (GENERATION_HISTORY, GENERATION_TABLE, ROLE_SCHEMAS, TABLE_PRIVILEGES,
                         ddl, grant_statements, role)
from .database import transaction_lock

IDENT = re.compile(r'^[a-z][a-z0-9_]{0,47}$')
REASON = re.compile(r'^[A-Za-z0-9:_-]{8,120}$')


def _connect(dsn: str, *, rehearsal: bool):
    import psycopg
    from psycopg.rows import dict_row
    validate_ha_dsn(dsn, allow_loopback=rehearsal, minimum_hosts=1 if rehearsal else 2)
    return psycopg.connect(dsn, row_factory=dict_row, target_session_attrs='read-write',
                           connect_timeout=5, application_name='pickup-ha-migrate')


def _schema(name: str, schema: str | None) -> str:
    value = schema or ROLE_SCHEMAS[role(name)]
    if not IDENT.fullmatch(value) or value == 'public' or value.startswith('pg_'):
        raise ValueError('explicit non-public schema required')
    return value


def apply(dsn: str, name: str, runtime_user: str, *, schema: str | None = None, limit_krw: int = 100000,
          rehearsal: bool = False) -> dict:
    with _connect(dsn, rehearsal=rehearsal) as db:
        return apply_on(db, name, runtime_user, schema=schema, limit_krw=limit_krw)


def apply_on(db, name: str, runtime_user: str, *, schema: str | None = None, limit_krw: int = 100000) -> dict:
    """Apply DDL and the reviewed grants on an owner connection (dict rows)."""
    from psycopg import sql
    schema = _schema(name, schema)
    if not IDENT.fullmatch(runtime_user):
        raise ValueError('explicit runtime user required')
    if type(limit_krw) is not int or limit_krw < 1:
        raise ValueError('positive synthetic credit limit required')
    with db.transaction():
        me = db.execute('SELECT current_user::text AS me').fetchone()['me']
        if me == runtime_user:
            raise ValueError('schema owner and runtime user must differ')
        if db.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (runtime_user,)).fetchone() is None:
            raise ValueError('runtime user is not provisioned')
        transaction_lock(db, 'schema-migration', schema)
        db.execute(sql.SQL('CREATE SCHEMA IF NOT EXISTS {} AUTHORIZATION CURRENT_USER').format(sql.Identifier(schema)))
        db.execute(sql.SQL('SET LOCAL search_path TO {}, pg_catalog').format(sql.Identifier(schema)))
        db.execute(ddl(name))
        if name == 'payment':
            db.execute('INSERT INTO payment_settings VALUES(%s,%s) ON CONFLICT DO NOTHING', ('limit_krw', limit_krw))
        tables = {row['relname'] for row in db.execute(
            '''SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
               WHERE n.nspname=%s AND c.relkind IN ('r','p','v','m','f')''', (schema,)).fetchall()}
        if tables != set(TABLE_PRIVILEGES[name]):
            raise ValueError('schema tables differ from the reviewed privilege contract')
        for statement in grant_statements(name, schema, runtime_user):
            db.execute(statement)
        generation = _generation(db, schema)
    return dict(role=name, schema=schema, runtime_user=runtime_user, tables=len(tables), generation=generation)


def _generation(db, schema: str) -> int:
    from psycopg import sql
    row = db.execute(sql.SQL('SELECT generation FROM {}.{} WHERE singleton').format(
        sql.Identifier(schema), sql.Identifier(GENERATION_TABLE))).fetchone()
    if row is None:
        raise ValueError('operating generation is not initialized')
    return row['generation']


def generation(dsn: str, name: str, *, schema: str | None = None, rehearsal: bool = False) -> int:
    schema = _schema(name, schema)
    with _connect(dsn, rehearsal=rehearsal) as db:
        return _generation(db, schema)


def bump_generation(dsn: str, name: str, *, expected: int, reason: str, schema: str | None = None,
                    rehearsal: bool = False) -> dict:
    with _connect(dsn, rehearsal=rehearsal) as db:
        return bump_on(db, name, expected=expected, reason=reason, schema=schema)


def bump_on(db, name: str, *, expected: int, reason: str, schema: str | None = None) -> dict:
    """Advance once per restore. Re-running with the same reason is a no-op."""
    from psycopg import sql
    schema = _schema(name, schema)
    if type(expected) is not int or expected < 1:
        raise ValueError('expected current generation required')
    if not isinstance(reason, str) or not REASON.fullmatch(reason):
        raise ValueError('bounded restore reason required')
    table, history = sql.Identifier(schema, GENERATION_TABLE), sql.Identifier(schema, GENERATION_HISTORY)
    with db.transaction():
        transaction_lock(db, 'operating-generation', schema)
        current = db.execute(sql.SQL('SELECT generation FROM {} WHERE singleton FOR UPDATE').format(table)).fetchone()
        if current is None:
            raise ValueError('operating generation is not initialized')
        done = db.execute(sql.SQL('SELECT generation,previous FROM {} WHERE reason=%s').format(history),
                          (reason,)).fetchone()
        if done is not None:
            if done['previous'] != expected or current['generation'] != done['generation']:
                raise ValueError('restore reason was already used for another generation change')
            return dict(role=name, generation=done['generation'], changed=False)
        if current['generation'] != expected:
            raise ValueError('operating generation changed concurrently; re-read before advancing')
        following = expected + 1
        db.execute(sql.SQL('UPDATE {} SET generation=%s,changed_at=clock_timestamp() WHERE singleton').format(table),
                   (following,))
        db.execute(sql.SQL('INSERT INTO {}(generation,previous,reason) VALUES(%s,%s,%s)').format(history),
                   (following, expected, reason))
    return dict(role=name, generation=following, changed=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    for command in ['apply', 'generation', 'bump-generation']:
        sub = commands.add_parser(command)
        sub.add_argument('--role', required=True, choices=sorted(ROLE_SCHEMAS))
        sub.add_argument('--schema')
        if command == 'apply':
            sub.add_argument('--runtime-user', required=True)
            sub.add_argument('--limit-krw', type=int, default=100000)
        if command == 'bump-generation':
            sub.add_argument('--expected', type=int, required=True)
            sub.add_argument('--reason', required=True)
    args = parser.parse_args(argv)
    dsn = os.environ.get('PICKUP_MIGRATE_DSN', '')
    rehearsal = os.environ.get('PICKUP_HA_REHEARSAL') == 'single_host_development'
    try:
        if args.command == 'apply':
            result = apply(dsn, args.role, args.runtime_user, schema=args.schema, limit_krw=args.limit_krw,
                           rehearsal=rehearsal)
        elif args.command == 'generation':
            result = dict(role=args.role, generation=generation(dsn, args.role, schema=args.schema, rehearsal=rehearsal))
        else:
            result = bump_generation(dsn, args.role, expected=args.expected, reason=args.reason,
                                     schema=args.schema, rehearsal=rehearsal)
    except (ValueError, OSError) as exc:
        # Validation messages never contain the DSN; database errors are reduced to SQLSTATE.
        print(json.dumps(dict(ok=False, error=type(exc).__name__, detail=str(exc)[:200])), file=sys.stderr)
        return 2
    except Exception as exc:
        import psycopg
        if isinstance(exc, psycopg.Error):
            print(json.dumps(dict(ok=False, error='database_error', sqlstate=getattr(exc, 'sqlstate', None))),
                  file=sys.stderr)
            return 3
        raise
    print(json.dumps(dict(ok=True, **result)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
