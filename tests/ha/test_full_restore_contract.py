from importlib.util import spec_from_file_location,module_from_spec
from pathlib import Path
import pytest


def test_full_recovery_probe_keeps_order_merchant_and_payment_evidence():
    path=Path(__file__).resolve().parents[2]/'scripts/ha_restore_journeys.py'
    assert path.exists(),'whole-journey restore probe is not implemented'
    spec=spec_from_file_location('ha_restore_journeys',path)
    module=module_from_spec(spec);spec.loader.exec_module(module)
    for name in ['seed_before_backup','prepare_target','verify_restored']:
        assert callable(getattr(module.JourneyRecoveryProbe,name,None))
