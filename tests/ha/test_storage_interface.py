"""Storage boundary is real, not a PostgreSQL-labelled SQLite connection."""
import importlib


def test_sqlite_provider_exposes_semantic_storage_boundary(tmp_path):
    from demo.route.payments.repository import PaymentRepository
    repo = PaymentRepository(tmp_path / 'payments.sqlite')
    for name in ('storage_ready', 'claim_notifications', 'finish_notification'):
        assert callable(getattr(repo, name, None)), f'missing provider operation: {name}'


def test_postgres_provider_module_exists_without_import_time_connections():
    try:
        importlib.import_module('demo.route.ha.payment_repository')
    except ModuleNotFoundError:
        assert False, 'native PostgreSQL payment repository is not implemented'
