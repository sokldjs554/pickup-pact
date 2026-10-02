"""Native PostgreSQL contract, separate sessions and actual HTTP (never SQLite)."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import importlib
import os
import threading
from uuid import uuid4
import pytest
from demo.route.payments.domain import PaymentError


def command(action='AUTHORIZE', **changes):
    c = dict(action=action, world_id='w-'+uuid4().hex, order_id='PCT-ONE',
             authorization_id='a-'+uuid4().hex, operation_key='k-'+uuid4().hex,
             amount_krw=2800, currency='KRW', payment_revision=1, quote_fingerprint='a'*64)
    if action=='AUTHORIZE': c['card_token']='demo-approved'
    return c | changes


def next_command(c, action, **changes):
    return {k:v for k,v in c.items() if k!='card_token'} | dict(action=action,operation_key='k-'+uuid4().hex) | changes


@pytest.fixture
def pg():
    dsn=os.environ.get('PICKUP_PG_TEST_DSN')
    if not dsn:
        pytest.skip('native PostgreSQL requires the explicitly configured CI/development database')
    assert os.environ.get('PICKUP_HA_TEST')=='1', 'explicit isolated-test opt-in required'
    try:
        module=importlib.import_module('demo.route.ha.payment_repository')
    except ModuleNotFoundError:
        pytest.fail('native PostgreSQL payment repository is not implemented')
    schema='pact_test_'+uuid4().hex
    repositories=[]
    def create(limit=100000):
        repo=module.PostgresPaymentRepository(dsn,schema=schema,limit_krw=limit)
        repositories.append(repo)
        return repo
    try:
        yield create
    finally:
        for repo in repositories: repo.close()
        import psycopg
        from psycopg import sql
        with psycopg.connect(dsn,autocommit=True) as db:
            db.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))


def test_native_storage_and_reopened_idempotency(pg):
    r=pg();c=command();first=r.execute(c);other=pg()
    assert r.storage_ready() and r.backend=='postgresql'
    assert other.execute(c)==first==other.operation(c['operation_key'])
    s=other.snapshot(c['world_id'],c['order_id'])
    assert s['held_krw']==2800 and s['capture_count']==0
    assert len(s['transactions'])==1
    assert 'card_token' not in s['authorizations'][0]


def test_replacement_capture_keeps_original_financial_history(pg):
    r=pg();c=command();r.execute(c)
    n=c|dict(authorization_id='replacement',operation_key='new-auth',amount_krw=3200,payment_revision=2,quote_fingerprint='b'*64)
    r.execute(n)
    assert r.snapshot(c['world_id'],c['order_id'])['held_krw']==6000
    r.execute(next_command(c,'VOID'));r.execute(next_command(n,'CAPTURE'))
    s=r.snapshot(c['world_id'],c['order_id'])
    assert (s['held_krw'],s['captured_krw'],s['capture_count'])==(0,3200,1)
    assert [t['kind'] for t in s['transactions']]==['AUTHORIZE','AUTHORIZE','VOID','CAPTURE']


@pytest.mark.parametrize('change',[{'amount_krw':3200},{'world_id':'different-world'}, {'order_id':'different-order'}, {'payment_revision':2}])
def test_same_key_different_payload_is_conflict_without_effect(pg,change):
    r=pg();c=command();r.execute(c);before=r.snapshot(c['world_id'],c['order_id'])
    with pytest.raises(PaymentError,match='IDEMPOTENCY_CONFLICT'): r.execute(c|change)
    assert r.snapshot(c['world_id'],c['order_id'])==before


def test_simultaneous_same_key_receives_one_original_receipt(pg):
    r=pg();s=pg();c=command()
    with ThreadPoolExecutor(max_workers=12) as pool:
        answers=list(pool.map(lambda i:(r if i%2 else s).execute(c),range(24)))
    assert all(x==answers[0] for x in answers)
    assert len(r.snapshot(c['world_id'],c['order_id'])['transactions'])==1


def test_different_keys_cannot_capture_order_twice(pg):
    r=pg();s=pg();c=command();r.execute(c)
    with ThreadPoolExecutor(max_workers=12) as pool:
        answers=list(pool.map(lambda i:(r if i%2 else s).execute(next_command(c,'CAPTURE')),range(24)))
    assert len({x['transaction_id'] for x in answers})==1
    final=r.snapshot(c['world_id'],c['order_id'])
    assert final['capture_count']==1 and final['captured_krw']==2800


def test_competing_authorizations_for_order_cannot_both_capture(pg):
    r=pg();s=pg();c=command();n=c|dict(authorization_id='a-replacement',operation_key='a-replacement',payment_revision=2)
    r.execute(c);s.execute(n)
    def capture(pair):
        repo,auth=pair
        try: return repo.execute(next_command(auth,'CAPTURE'))
        except PaymentError as exc:return {'ok':False,'code':exc.code}
    with ThreadPoolExecutor(max_workers=2) as pool:
        out=list(pool.map(capture,[(r,c),(s,n)]))
    assert sum(x['ok'] for x in out)==1
    assert [x['code'] for x in out if not x['ok']]==['ORDER_ALREADY_CAPTURED']
    assert r.snapshot(c['world_id'],c['order_id'])['capture_count']==1


def test_credit_limit_race_is_guarded_across_different_orders(pg):
    r=pg(limit=5900);s=pg(limit=5900);world='shared-wallet-'+uuid4().hex
    commands=[command(world_id=world,order_id=f'ORDER-{i}',amount_krw=3000) for i in range(12)]
    with ThreadPoolExecutor(max_workers=12) as pool:
        out=list(pool.map(lambda i:(r if i%2 else s).execute(commands[i]),range(12)))
    assert sum(x['ok'] for x in out)==1
    assert all(x['code']=='LIMIT_EXCEEDED' for x in out if not x['ok'])
    assert sum(r.snapshot(world,c['order_id'])['held_krw'] for c in commands)==3000


def test_decline_does_not_create_authorization_or_outbox(pg):
    r=pg();c=command(card_token='demo-declined');a=r.execute(c)
    assert not a['ok'] and a['code']=='DECLINED'
    assert r.execute(c)==a and not r.snapshot(c['world_id'],c['order_id'])['transactions']
    assert r.claim_notifications('worker-a')==[]


def test_voided_authorization_cannot_be_revived(pg):
    r=pg();c=command();original=r.execute(c);r.execute(next_command(c,'VOID'))
    assert r.execute(c)==original
    with pytest.raises(PaymentError,match='AUTHORIZATION_NOT_ACTIVE'):r.execute(c|{'operation_key':'fresh'})
    with pytest.raises(PaymentError,match='AUTHORIZATION_NOT_ACTIVE'):r.execute(next_command(c,'CAPTURE'))
    assert r.snapshot(c['world_id'],c['order_id'])['held_krw']==0


@pytest.mark.parametrize('bad',[True,False,0,-1,2.8,'2800',None,30001])
def test_invalid_money_never_enters_postgres(pg,bad):
    r=pg();c=command(amount_krw=bad)
    with pytest.raises(PaymentError): r.execute(c)
    assert not r.snapshot(c['world_id'],c['order_id'])['transactions']


def test_other_world_cannot_rebind_an_authorization(pg):
    r=pg();c=command();r.execute(c)
    with pytest.raises(PaymentError,match='AUTHORIZATION_BINDING_CONFLICT'):
        r.execute(next_command(c,'CAPTURE',world_id='other-world'))
    assert r.snapshot('other-world',c['order_id'])['transactions']==[]
    assert r.snapshot(c['world_id'],c['order_id'])['capture_count']==0


def test_outbox_lease_takeover_fences_a_late_success(pg):
    r=pg();s=pg();c=command();r.execute(c)
    old=r.claim_notifications('worker-a',lease_seconds=10)[0]
    assert s.claim_notifications('worker-b')==[]
    with r.database.transaction() as db:
        db.execute('UPDATE payment_outbox SET lease_until=clock_timestamp()-interval \'1 second\' WHERE event_id=%s',(old['event_id'],))
    new=s.claim_notifications('worker-b')[0]
    assert new['lease_version']>old['lease_version']
    assert r.finish_notification(old,True) is False
    assert s.finish_notification(new,True) is True
    assert s.finish_notification(new,True) is False
    assert r.claim_notifications('worker-c')==[]


def test_outbox_workers_cannot_claim_same_event(pg):
    r=pg();s=pg();c=command();r.execute(c)
    with ThreadPoolExecutor(max_workers=2) as pool:
        batches=list(pool.map(lambda p:p[0].claim_notifications(p[1]),[(r,'one'),(s,'two')]))
    assert sum(map(len,batches))==1
    claim=next(b[0] for b in batches if b)
    assert r.finish_notification(claim,False)
    assert r.claim_notifications('three')==[]


def test_replay_after_reply_is_discarded_preserves_transaction_and_fault(pg):
    r=pg();c=command();original=r.execute(c)
    assert r.consume_fault(c['world_id'],c['operation_key'],'drop_reply')
    s=pg();assert not s.consume_fault(c['world_id'],c['operation_key'],'drop_reply')
    assert s.operation(c['operation_key'])==original
    assert s.execute(c)==original


def test_locked_wallet_does_not_block_another_world(pg):
    r=pg();s=pg();c=command();r.execute(c)
    from demo.route.ha.database import transaction_lock
    with r.database.transaction() as db:
        transaction_lock(db,'payment-wallet',c['world_id'])
        with ThreadPoolExecutor(max_workers=1) as pool:
            answer=pool.submit(s.execute,command(world_id='independent-'+uuid4().hex)).result(timeout=2)
    assert answer['ok']


def test_pool_replaces_dead_connection_without_losing_data(pg):
    r=pg();c=command();r.execute(c)
    with r.database.transaction() as db: pid=db.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid']
    import psycopg
    with psycopg.connect(os.environ['PICKUP_PG_TEST_DSN'],autocommit=True) as admin:
        assert admin.execute('SELECT pg_terminate_backend(%s)',(pid,)).fetchone()[0]
    assert r.snapshot(c['world_id'],c['order_id'])['held_krw']==2800
