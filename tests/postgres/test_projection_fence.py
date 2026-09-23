"""Real PostgreSQL tests; a dedicated CI DB is required (never a mock DB)."""
from copy import deepcopy
import importlib
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.models import ReconcileRequest
from app.engine import reconcile
from app import persistence

pytestmark = pytest.mark.skipif(not os.getenv('POSTGRES_DSN'), reason='requires dedicated PostgreSQL')
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope='module', autouse=True)
def initialize_database():
    if not os.getenv('POSTGRES_DSN'):
        return
    import psycopg
    with psycopg.connect(os.environ['POSTGRES_DSN']) as db:
        db.execute((ROOT/'sql/schema.sql').read_text())
        for path in sorted((ROOT/'sql/migrations').glob('*.sql')):
            db.execute(path.read_text())


def packet():
    aggregate = str(uuid4())
    events=[]
    for n, (key,kind,cause) in enumerate([
        ('hold','PickupSlotHeld',None), ('pay','PaymentAuthorized','hold'),
        ('confirm','CommitmentConfirmed','pay'), ('cancel','CommitmentCancelled','confirm')]):
        events.append({'event_id':key,'aggregate_id':aggregate,'event_type':kind,
                       'occurred_at':f'2026-09-23T10:00:0{n}Z',
                       'received_at':f'2026-09-23T10:00:0{n}Z',
                       'causation_id':cause,'payload':{}})
    return {'events':events}


def test_historical_packet_cannot_erase_registered_cancellation():
    client=TestClient(app)
    full=packet(); old={'events':full['events'][:3]}
    assert client.post('/api/v1/projections/rebuild',json=full).status_code==200
    assert client.post('/api/v1/projections/rebuild',json=old).status_code==200
    latest=client.get('/api/v1/projections/'+full['events'][0]['aggregate_id']).json()['projection']
    assert latest['status']=='CANCELLED', latest


def test_unfenced_result_is_rejected_even_if_currently_valid():
    result=reconcile(ReconcileRequest(**packet()))
    with pytest.raises(ValueError, match='evidence'):
        persistence.rebuild_projection(result)


def store_module():
    assert importlib.util.find_spec('app.projection_store'), 'server-owned evidence fence is missing'
    return importlib.import_module('app.projection_store')


def test_delayed_worker_cannot_overwrite_newer_publication():
    store=store_module(); full=packet()
    old=store.capture_evidence(ReconcileRequest(events=full['events'][:3]))
    new=store.capture_evidence(ReconcileRequest(**full))
    store.publish_projection(new)
    with pytest.raises(store.StaleProjection):
        store.publish_projection(old)
    latest=persistence.get_projection(full['events'][0]['aggregate_id'])
    assert latest['status']=='CANCELLED'
    assert latest['source_revision']==new.revision
    assert latest['is_current'] is True


def test_same_semantic_redelivery_keeps_revision_and_digest():
    store=store_module(); full=packet()
    first=store.capture_evidence(ReconcileRequest(**full))
    repeat=deepcopy(full)
    for e in repeat['events']:
        e['received_at']='2026-09-23T11:30:00Z'
    retry=store.capture_evidence(ReconcileRequest(**repeat))
    assert (first.revision,first.evidence_hash)==(retry.revision,retry.evidence_hash)
    a=store.publish_projection(first); b=store.publish_projection(retry)
    assert a['canonical_hash']==b['canonical_hash']


def test_new_conflict_makes_existing_projection_stale_and_blocks_publication():
    store=store_module(); full=packet()
    first=store.capture_evidence(ReconcileRequest(**full))
    store.publish_projection(first)
    conflicting=deepcopy(full['events'][-1]); conflicting['payload']={'reason':'different'}
    conflict=store.capture_evidence(ReconcileRequest(events=[conflicting]))
    assert conflict.result.decision=='MANUAL_REVIEW'
    with pytest.raises(ValueError): store.publish_projection(conflict)
    latest=persistence.get_projection(full['events'][0]['aggregate_id'])
    assert latest['is_current'] is False
    assert latest['source_revision']==first.revision
    assert latest['evidence_revision']==conflict.revision


def test_concurrent_historical_packets_never_remove_facts():
    store=store_module(); full=packet()
    first=store.capture_evidence(ReconcileRequest(**full)); store.publish_projection(first)
    def replay(n):
        candidate=store.capture_evidence(ReconcileRequest(events=full['events'][:(n%4)+1]))
        return store.publish_projection(candidate)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(replay,range(24)))
    assert {r['status'] for r in results}=={'CANCELLED'}
    assert {r['source_revision'] for r in results}=={first.revision}


def test_candidate_forgery_cannot_authorize_another_result():
    from dataclasses import replace
    store=store_module(); full=packet()
    candidate=store.capture_evidence(ReconcileRequest(**full))
    bad=reconcile(ReconcileRequest(events=full['events'][:3]))
    forged=replace(candidate,result=bad)
    with pytest.raises(ValueError): store.publish_projection(forged)
