"""Backwards compatible SQLite outbox leases, also used by the common sender."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import sqlite3
import time
import pytest
from demo.route.payments.repository import PaymentRepository
from demo.route.payments.notifications import deliver_due,sign


def approval():
    return dict(action='AUTHORIZE',world_id='world',order_id='ORDER',operation_key='APPROVE',authorization_id='AUTH',
                amount_krw=2800,currency='KRW',payment_revision=1,quote_fingerprint='a'*64,card_token='demo-approved')


def test_only_one_worker_claims_same_sqlite_event(tmp_path):
    path=tmp_path/'payments.sqlite';a=PaymentRepository(path);b=PaymentRepository(path);a.execute(approval())
    assert hasattr(a,'claim_notifications'), 'semantic outbox claims are missing'
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims=list(pool.map(lambda p:p[0].claim_notifications(p[1]),[(a,'one'),(b,'two')]))
    assert sum(map(len,claims))==1
    row=next(c[0] for c in claims if c)
    assert a.finish_notification(row,True)
    assert not b.finish_notification(row,True)


def test_expired_owner_cannot_ack_after_takeover(tmp_path):
    r=PaymentRepository(tmp_path/'pg.sqlite');r.execute(approval())
    assert hasattr(r,'claim_notifications'), 'lease fencing is missing'
    old=r.claim_notifications('old')[0]
    with r.connection() as db:db.execute('UPDATE payment_outbox SET lease_until=0')
    new=r.claim_notifications('new')[0]
    assert new['lease_version']>old['lease_version']
    assert not r.finish_notification(old,True)
    assert r.finish_notification(new,True)


def test_sender_delivers_original_body_twice_and_records_one_ack(tmp_path):
    r=PaymentRepository(tmp_path/'pg.sqlite');r.execute(approval(),notification_copies=2)
    sent=[];secret='synthetic-test-notification-secret'
    def send(body,stamp,signature):
        assert signature==sign(secret,stamp,body)
        sent.append(body);return True
    assert deliver_due(r,secret,send)==1
    assert len(sent)==2 and sent[0]==sent[1]
    assert deliver_due(r,secret,send)==0
    assert r.snapshot('world','ORDER')['held_krw']==2800


def test_failed_notification_preserves_event_for_bounded_retry(tmp_path):
    r=PaymentRepository(tmp_path/'pg.sqlite');r.execute(approval())
    assert deliver_due(r,'synthetic-test-notification-secret',lambda *args:False)==0
    with r.connection() as db:
        row=db.execute('SELECT * FROM payment_outbox').fetchone()
    assert row['attempts']==1 and row['delivered']==0
    assert row['next_at']>time.time()
    assert r.snapshot('world','ORDER')['held_krw']==2800
