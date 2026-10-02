"""The destructive rehearsal has no production target argument."""
import importlib.util
from pathlib import Path
import pytest


def runner():
    path=Path(__file__).resolve().parents[2]/'scripts/verify_ha_restore.py'
    assert path.is_file(),'isolated restore rehearsal is missing'
    spec=importlib.util.spec_from_file_location('restore_runner',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def test_rehearsal_requires_explicit_local_test_opt_in(tmp_path,monkeypatch):
    module=runner();monkeypatch.delenv('PICKUP_HA_TEST',raising=False)
    with pytest.raises(ValueError,match='isolated'):module.rehearse(tmp_path)


def test_rehearsal_exposes_no_production_dsn_or_volume_override():
    import inspect
    module=runner()
    assert list(inspect.signature(module.rehearse).parameters)==['output']
    assert '@sha256:' in module.IMAGE
