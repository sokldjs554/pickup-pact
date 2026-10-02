"""Network restore rehearsal cannot accept an existing account or volume."""
import importlib.util
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[2]


def load(name):
    path = ROOT/'scripts'/f'{name}.py'
    assert path.exists(), 'encrypted network rehearsal has not been implemented'
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_remote_lab_requires_explicit_test_opt_in(monkeypatch):
    monkeypatch.delenv('PICKUP_HA_TEST', raising=False)
    with pytest.raises(ValueError, match='isolated'):
        load('ha_restic_lab').ResticLab()


def test_remote_lab_has_no_existing_repository_or_host_override():
    import inspect
    assert list(inspect.signature(load('ha_restic_lab').ResticLab).parameters) == []


def test_restore_transport_selection_is_closed_before_database_access(tmp_path, monkeypatch):
    monkeypatch.setenv('PICKUP_HA_TEST', '1')
    monkeypatch.setenv('PICKUP_BACKUP_TRANSPORT', 'https://production.example')
    with pytest.raises(ValueError, match='transport'):
        load('verify_ha_restore').rehearse(tmp_path/'never-created')
    assert not (tmp_path/'never-created').exists()
