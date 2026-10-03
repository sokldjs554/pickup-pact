"""The pool must notice a silently partitioned database leader on the client side."""
import sys
import types

import pytest

pytest.importorskip('psycopg')
pytest.importorskip('psycopg_pool')

from demo.route.ha import database  # noqa: E402


def test_pool_connections_detect_a_black_holed_leader(monkeypatch):
    seen = {}

    class FakePool:
        def __init__(self, dsn, **options):
            seen.update(options)
            raise RuntimeError('stop after capturing the options')

    monkeypatch.setattr('psycopg_pool.ConnectionPool', FakePool)
    with pytest.raises(RuntimeError):
        database.PostgresDatabase('host=a,b port=5432,5432 dbname=x user=u', schema='pact_orders')
    kwargs = seen['kwargs']
    assert kwargs['keepalives'] == 1 and kwargs['keepalives_idle'] <= 10
    assert kwargs['keepalives_interval'] <= 5 and kwargs['keepalives_count'] <= 5
    assert 1000 <= kwargs['tcp_user_timeout'] <= 15000
    assert kwargs['target_session_attrs'] == 'read-write' and kwargs['connect_timeout'] <= 5
