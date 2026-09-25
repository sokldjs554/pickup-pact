"""Real PostgreSQL regression: a late old replay must not replace newer evidence.

This suite is mandatory in projection-fence CI. It is separate from stateless
unit tests; no fake cursor or SQLite substitutes for PostgreSQL locking.
"""
import os
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from app.main import app

pytestmark = pytest.mark.skipif(
    not os.getenv('PROJECTION_TEST_DSN'), reason='requires disposable PostgreSQL (projection-fence CI)'
)


@pytest.fixture(scope='module', autouse=True)
def schema():
    import psycopg
    with psycopg.connect(os.environ['PROJECTION_TEST_DSN']) as db:
        db.execute((Path(__file__).resolve().parents[3]/'sql/schema.sql').read_text())


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('POSTGRES_DSN', os.environ['PROJECTION_TEST_DSN'])
    with TestClient(app) as c:
        yield c


def packet():
    aggregate = 'projection-fence-' + uuid4().hex
    start = datetime(2026, 9, 23, tzinfo=UTC)
    events = []
    for n, (identity, kind, cause) in enumerate([
        ('hold','PickupSlotHeld',None), ('pay','PaymentAuthorized','hold'),
        ('confirm','CommitmentConfirmed','pay'), ('cancel','CommitmentCancelled','confirm')
    ]):
        events.append({'event_id':identity, 'aggregate_id':aggregate, 'event_type':kind,
                       'occurred_at':(start+timedelta(seconds=n)).isoformat(),
                       'received_at':(start+timedelta(seconds=n)).isoformat(),
                       'causation_id':cause, 'payload':{}})
    return events


def post(client, events, expected):
    return client.post('/api/v1/projections/rebuild', json={'events':events, 'expected_revision':expected})


def stored(client, events):
    response=client.get('/api/v1/projections/'+events[0]['aggregate_id'])
    assert response.status_code==200, response.text
    return response.json()['projection']


def test_late_old_result_cannot_overwrite_cancelled_projection(client):
    events=packet()
    assert post(client,events[:3],0).status_code==200
    assert post(client,events,1).status_code==200
    late_old=post(client,events[:3],1)
    assert late_old.status_code==409, 'old successful result was accepted: '+late_old.text
    assert stored(client,events)['status']=='CANCELLED'


def test_fresh_revision_does_not_authorize_dropping_known_cancel(client):
    events=packet()
    assert post(client,events[:3],0).status_code==200
    assert post(client,events,1).status_code==200
    missing_cancel=post(client,events[:3],2)
    assert missing_cancel.status_code==409, 'fresh token must not authorize an old packet: '+missing_cancel.text
    assert stored(client,events)['status']=='CANCELLED'


def test_known_event_content_cannot_change_under_same_identity(client):
    events=packet()
    assert post(client,events,0).status_code==200
    altered=deepcopy(events)
    altered[1]['payload']={'authorization_id':'different-authority'}
    changed=post(client,altered,1)
    assert changed.status_code==409, 'accepted evidence identity was rewritten: '+changed.text


def test_safe_write_without_revision_precondition_is_rejected(client):
    events=packet()
    response=client.post('/api/v1/projections/rebuild',json={'events':events})
    assert response.status_code==428, response.text
    assert client.get('/api/v1/projections/'+events[0]['aggregate_id']).status_code==404
