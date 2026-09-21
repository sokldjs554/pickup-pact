from __future__ import annotations

import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "app.py"
spec = importlib.util.spec_from_file_location("pickup_pact_ops_console", MODULE_PATH)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
app = module.app


def test_home_explains_product_in_korean() -> None:
    client = app.test_client()
    response = client.get("/")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "취소가 늦게 도착해도" in body
    assert "30초 안에 이해하는 백엔드" in body
    assert "합성 정합성 벤치마크" in body


def test_demo_run_uses_reconciler_result(monkeypatch) -> None:
    def fake_post(path: str, payload: dict) -> dict:
        assert path == "/api/v1/replay"
        assert payload["events"]
        return {
            "reconciliation": {
                "aggregate_id": "order-late-cancel",
                "receive_order_state": {"status": "CANCELLED", "payment_authorized": True, "settled": True, "rewarded": True, "capacity_revision": 0, "settlement_post_count": 1, "reward_post_count": 1},
                "canonical_state": {"status": "CANCELLED", "payment_authorized": True, "settled": True, "rewarded": True, "capacity_revision": 0, "settlement_post_count": 1, "reward_post_count": 1},
                "anomalies": ["settlement_posted_after_prior_cancellation", "reward_granted_after_prior_cancellation"],
                "repairs": ["REVERSE_SETTLEMENT", "REVERSE_REWARD"],
                "duplicate_event_ids": [],
                "evidence_event_ids": ["cancel", "settlement", "reward"],
            },
            "ai_review": {"provider": "offline", "summary": "2 anomalies", "hypotheses": [], "evidence_event_ids": ["cancel"], "advisory_only": True},
        }

    monkeypatch.setattr(module, "post_json", fake_post)
    client = app.test_client()
    response = client.post("/api/demo/run/late-cancel")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["scenario"] == "late-cancel"
    assert "REVERSE_SETTLEMENT" in payload["result"]["reconciliation"]["repairs"]


def test_unknown_scenario_is_404() -> None:
    client = app.test_client()
    response = client.post("/api/demo/run/not-a-scenario")
    assert response.status_code == 404
