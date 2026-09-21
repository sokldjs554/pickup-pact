from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_reconcile_validation_and_response():
    payload = {
        "events": [
            {
                "event_id": "hold",
                "aggregate_id": "order-1",
                "event_type": "PickupSlotHeld",
                "occurred_at": "2026-09-18T03:00:00Z",
                "received_at": "2026-09-18T03:00:00Z",
                "payload": {"capacity_units": 1},
            },
            {
                "event_id": "pay",
                "aggregate_id": "order-1",
                "event_type": "PaymentAuthorized",
                "occurred_at": "2026-09-18T03:00:01Z",
                "received_at": "2026-09-18T03:00:01Z",
                "payload": {},
            },
            {
                "event_id": "confirm",
                "aggregate_id": "order-1",
                "event_type": "CommitmentConfirmed",
                "occurred_at": "2026-09-18T03:00:02Z",
                "received_at": "2026-09-18T03:00:02Z",
                "payload": {},
            },
        ]
    }
    response = client.post("/api/v1/reconcile", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["canonical_state"]["status"] == "CONFIRMED"
    assert body["repairs"] == []


def test_replay_marks_ai_as_advisory():
    payload = {
        "events": [
            {
                "event_id": "cancel",
                "aggregate_id": "order-2",
                "event_type": "CommitmentCancelled",
                "occurred_at": "2026-09-18T03:00:05Z",
                "received_at": "2026-09-18T03:00:50Z",
                "payload": {},
            },
            {
                "event_id": "settle",
                "aggregate_id": "order-2",
                "event_type": "SettlementPosted",
                "occurred_at": "2026-09-18T03:00:10Z",
                "received_at": "2026-09-18T03:00:10Z",
                "payload": {"amount": "12000"},
            },
        ]
    }
    response = client.post("/api/v1/replay", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["ai_review"]["advisory_only"] is True
    assert body["ai_review"]["provider"] == "offline"
    assert "REVERSE_SETTLEMENT" in body["reconciliation"]["repairs"]


def test_reconcile_rejects_multiple_aggregates():
    payload = {
        "events": [
            {
                "event_id": "a", "aggregate_id": "order-a", "event_type": "PickupSlotHeld",
                "occurred_at": "2026-09-18T03:00:00Z", "received_at": "2026-09-18T03:00:00Z", "payload": {}
            },
            {
                "event_id": "b", "aggregate_id": "order-b", "event_type": "PaymentAuthorized",
                "occurred_at": "2026-09-18T03:00:01Z", "received_at": "2026-09-18T03:00:01Z", "payload": {}
            },
        ]
    }
    response = client.post("/api/v1/reconcile", json=payload)
    assert response.status_code == 422
