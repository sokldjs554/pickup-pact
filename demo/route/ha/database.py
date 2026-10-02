"""Bounded PostgreSQL transactions; SQL belongs to each explicit repository."""
from __future__ import annotations
from contextlib import contextmanager
import hashlib
import re


class StorageUnavailable(OSError):
    """An unavailable/uncertain database operation, never permission for a new key."""


def transaction_lock(db, namespace: str, identity: str) -> None:
    # Stable, transaction-scoped and shared by all processes. A rare hash
    # collision can only serialize extra work, not weaken exclusion.
    digest=hashlib.sha256((namespace+'\0'+identity).encode()).digest()
    number=int.from_bytes(digest[:8], 'big', signed=True)
    db.execute('SELECT pg_advisory_xact_lock(%s)', (number,))


class PostgresDatabase:
    def __init__(self, dsn: str, *, schema: str, maximum_connections: int=8, runtime_guard=None):
        if (not isinstance(schema,str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,47}',schema)
                or schema=='public' or schema.startswith('pg_')):
            raise ValueError('explicit non-public repository schema required')
        if type(maximum_connections) is not int or not 1<=maximum_connections<=32:
            raise ValueError('bounded connection count required')
        if not isinstance(dsn,str) or not dsn.strip():
            raise ValueError('PostgreSQL connection configuration required')
        # Optional dependencies do not affect the existing SQLite runtime.
        import psycopg
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool, PoolTimeout
        self._driver,self._pool_timeout=psycopg,PoolTimeout
        self.schema=schema
        # ha_postgres_v1: least-privilege and operating-generation checks.
        self.runtime_guard=runtime_guard
        self._generation_verified=False
        self._pool=ConnectionPool(dsn,min_size=1,max_size=maximum_connections,
            open=False,timeout=5,max_waiting=64,max_lifetime=120,
            kwargs={'autocommit':True,'row_factory':dict_row,'connect_timeout':3,
                    'target_session_attrs':'read-write','application_name':'pickup-native-storage'},
            check=self._check)
        try:
            self._pool.open(wait=True,timeout=8)
            with self._pool.connection() as db:
                if db.info.server_version//10000 != 17:
                    raise ValueError('this storage contract is validated for PostgreSQL 17')
                if runtime_guard is not None:
                    runtime_guard.verify_privileges(db,schema)
                    runtime_guard.check_generation(db,schema)
                    self._generation_verified=True
        except Exception as exc:
            self._pool.close()
            from .privileges import GenerationMismatch, PrivilegeViolation
            if isinstance(exc,(GenerationMismatch,PrivilegeViolation)):
                raise  # a configuration error, never a transient outage
            raise StorageUnavailable('PostgreSQL storage is not ready') from None

    def _check(self,db):
        from psycopg import OperationalError
        status=db.execute("SELECT pg_is_in_recovery() AS recovering, current_setting('transaction_read_only') AS readonly").fetchone()
        if status['recovering'] or status['readonly']!='off':
            raise OperationalError('writable leader connection required')
        if self.runtime_guard is not None and self._generation_verified:
            # Every checkout, so a restored database of a later generation is
            # never written by a process configured for the earlier one.
            self.runtime_guard.check_generation(db,self.schema)

    @contextmanager
    def transaction(self, *, readonly: bool=False):
        from psycopg import sql
        try:
            with self._pool.connection() as db:
                with db.transaction():
                    if readonly:
                        db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                    db.execute(sql.SQL('SET LOCAL search_path TO {}, pg_catalog').format(sql.Identifier(self.schema)))
                    db.execute("SET LOCAL lock_timeout='5s'")
                    db.execute("SET LOCAL statement_timeout='10s'")
                    db.execute("SET LOCAL idle_in_transaction_session_timeout='15s'")
                    yield db
        except (self._driver.Error,self._pool_timeout):
            # A caller must reconcile an uncertain COMMIT using the SAME key.
            # Do not log the DSN, raw server error, or automatically redo effects.
            raise StorageUnavailable('PostgreSQL operation could not be confirmed') from None

    def initialize(self, ddl: str) -> None:
        from psycopg import sql
        with self.transaction() as db:
            transaction_lock(db,'schema-migration',self.schema)
            db.execute(sql.SQL('CREATE SCHEMA IF NOT EXISTS {}').format(sql.Identifier(self.schema)))
            db.execute(ddl)

    def close(self):
        self._pool.close()
