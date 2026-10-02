"""Actual PostgreSQL work ownership; no result is applied by an expired worker."""
import importlib
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import pytest


def test_operation_storage_module_is_an_explicit_boundary():
    try: importlib.import_module('demo.route.ha.operation_store')
    except ModuleNotFoundError: pytest.fail('native operation storage is missing')


@pytest.fixture
def work():
    dsn=os.environ.get('PICKUP_PG_TEST_DSN')
    if not dsn: pytest.skip('isolated PostgreSQL required')
    assert os.environ.get('PICKUP_HA_TEST')=='1'
    from demo.route.ha.operation_store import PostgresOperationStore
    from psycopg import connect,sql
    schema='pact_work_'+uuid4().hex
    stores=[]
    def make():
        s=PostgresOperationStore(dsn,schema=schema);stores.append(s);return s
    try: yield make
    finally:
        for s in stores:s.close()
        with connect(dsn,autocommit=True) as db:db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def record(s, oid='op-1',order='order-1'):
    return s.enqueue(oid,order,{'phase':'PAY_CAPTURE','external_key':'original-capture'},request_key='request-'+oid)


def test_same_request_is_immutable_and_shared(work):
    a=work();b=work();original=record(a)
    assert record(b)==original
    from demo.route.ha.operation_store import OperationConflict
    with pytest.raises(OperationConflict):
        b.enqueue('different-op','order-1',{'phase':'OTHER'},request_key='request-op-1')
    assert a.read('op-1')['payload']=={'phase':'PAY_CAPTURE','external_key':'original-capture'}


def test_multiple_workers_claim_once(work):
    a=work();b=work();record(a)
    with ThreadPoolExecutor(max_workers=2) as pool:
        batches=list(pool.map(lambda p:p[0].claim_due(p[1]),[(a,'worker-a'),(b,'worker-b')]))
    assert sum(map(len,batches))==1


def test_takeover_fences_old_result_and_preserves_external_key(work):
    a=work();b=work();record(a);old=a.claim_due('old')[0]
    with a.database.transaction() as db:db.execute("UPDATE ha_operations SET lease_until=clock_timestamp()-interval '1 second' WHERE id='op-1'")
    new=b.claim_due('new')[0]
    assert new['lease_version']>old['lease_version']
    assert new['payload']['external_key']==old['payload']['external_key']
    assert not a.apply_result(old,{'phase':'DONE'},finished=True)
    assert b.apply_result(new,{'phase':'CLAIM','external_key':'original-claim'},finished=False)
    assert not b.apply_result(new,{'phase':'BAD'},finished=True)
    row=a.read('op-1')
    assert row['phase_version']==1 and row['status']=='PENDING' and row['payload']['phase']=='CLAIM'


def test_expired_lease_rejected_without_takeover(work):
    a=work();record(a);old=a.claim_due('old')[0]
    with a.database.transaction() as db:db.execute("UPDATE ha_operations SET lease_until=clock_timestamp()-interval '1 second' WHERE id='op-1'")
    assert not a.apply_result(old,{'phase':'DONE'},finished=True)
    assert a.read('op-1')['status']=='PENDING'


def test_locked_operation_does_not_block_other_order(work):
    a=work();b=work();record(a);record(a,'op-2','order-2')
    with a.database.transaction() as db:
        db.execute("SELECT id FROM ha_operations WHERE id='op-1' FOR UPDATE")
        with ThreadPoolExecutor(max_workers=1) as pool:
            batch=pool.submit(b.claim_due,'other').result(timeout=2)
    assert [r['id'] for r in batch]==['op-2']


def test_active_operation_per_order_and_finished_replay(work):
    a=work();record(a)
    from demo.route.ha.operation_store import OperationConflict
    with pytest.raises(OperationConflict):record(a,'op-2','order-1')
    claim=a.claim_due('one')[0]
    assert a.apply_result(claim,{'phase':'DONE'},finished=True)
    assert a.claim_due('two')==[]
    assert record(a)['id']=='op-1'
    assert record(a,'op-2','order-1')['id']=='op-2'
