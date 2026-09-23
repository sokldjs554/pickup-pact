from pathlib import Path
import yaml

ROOT=Path(__file__).resolve().parents[3]


def test_aggregate_contract_declares_projection_safety_conflict():
    contract=yaml.safe_load((ROOT/'contracts/openapi.yaml').read_text())
    responses=contract['paths']['/api/v1/projections/rebuild']['post']['responses']
    assert '409' in responses
    result=contract['paths']['/api/v1/reconcile']['post']['responses']['200']
    assert result['content']['application/json']['schema']['$ref'].endswith('/ReconcileResult')


def test_service_contract_covers_capacity_input_validation():
    contract=yaml.safe_load((ROOT/'contracts/repair-evidence.openapi.yaml').read_text())
    payload=contract['components']['schemas']['EventEnvelope']['properties']['payload']
    assert payload['properties']['capacity_units'] == {'type':'integer','minimum':1}
    assert payload['properties']['available_units'] == {'type':'integer','minimum':0}
