"""Real PostgreSQL journey/merchant parity before the new backend is selected."""
from concurrent.futures import ThreadPoolExecutor
import importlib
import os
from uuid import uuid4
import pytest
from demo.route.store import Conflict


def native(name):
    try:
        return importlib.import_module('demo.route.ha.' + name)
    except ModuleNotFoundError:
        pytest.fail('native journey and merchant integration is not implemented: ' + name)


def test_native_modules_import_without_creating_connections():
    native('merchant_repository')
    native('journey_store')


@pytest.fixture
def integrated():
    dsn = os.environ.get('PICKUP_PG_TEST_DSN')
    if not dsn:
        pytest.skip('explicit isolated PostgreSQL required')
    assert os.environ.get('PICKUP_HA_TEST') == '1'
    Merchant = native('merchant_repository').PostgresMerchantFleet
    Store = native('journey_store').PostgresJourneyStore
    from demo.route.ha.payment_repository import PostgresPaymentRepository
    from demo.route.ha.local_gateway import RepositoryPaymentClient
    schemas = ['pact_int_' + uuid4().hex for _ in range(3)]
    merchants = [Merchant(dsn, schema=schemas[0]) for _ in range(2)]
    pg = PostgresPaymentRepository(dsn, schema=schemas[1])
    client = RepositoryPaymentClient(pg)
    stores = [Store(dsn, schema=schemas[2], fleet=merchants[i], payment_gateway=client,
                    notification_secret='integration-secret-32-characters', worker_id='app-'+str(i)) for i in range(2)]
    try:
        yield stores, merchants, pg
    finally:
        for item in stores + merchants + [pg]:
            item.close()
        import psycopg
        from psycopg import sql
        with psycopg.connect(dsn, autocommit=True) as db:
            for schema in schemas:
                db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def fresh(store, **extra):
    from demo.route.api import Intent
    return store.create(Intent(points=1000, coupon_id='welcome500').model_dump(), **extra)


def quote(s, shop):
    return next(p for p in s['all_plans'] if p['store_id'] == shop)['quote_id']


def cmd(st, s, action, **kw):
    return st.command(s['id'], dict(action=action, request_id=uuid4().hex,
                                   expected_version=s['version'], **kw))


def picked(st, s):
    s = cmd(st, s, 'advance', minutes=5)
    s = cmd(st, s, 'start')
    s = cmd(st, s, 'advance', minutes=5)
    s = cmd(st, s, 'ready')
    return cmd(st, s, 'claim', pickup_code=s['order']['pickup_code'])


def test_two_apps_share_same_request_order_and_final_proof(integrated):
    stores, merchants, pg = integrated
    a, b = stores
    s = fresh(a)
    c = dict(action='reserve', request_id='same-order', expected_version=s['version'], quote_id=quote(s, 'wave'))
    s = a.command(s['id'], c)
    again = b.command(s['id'], c)
    assert again['duplicate'] and again['order'] == s['order']
    oid = s['order']['id']
    s = cmd(b, s, 'transfer', quote_id=quote(s, 'oat'))
    assert s['order']['id'] == oid and s['order']['price'] == 3200
    s = picked(a, s)
    from demo.route.reconciliation import reconcile
    proof = reconcile(b, s['id'])
    assert proof['status'] == 'MATCH' and proof['terminal']
    assert proof['payment']['captured_krw'] == 3200 and proof['payment']['capture_count'] == 1
    assert proof['payment']['held_krw'] == 0
    assert s['wallet']['spent'] == 1000 and s['wallet']['earned'] == 32
    assert merchants[1].evidence(s['world_id'], oid)['merchants']['wave']['reservation']['phase'] == 'RELEASED'


def test_concurrent_same_reservation_across_instances_is_one_order(integrated):
    stores, _, pg = integrated
    s = fresh(stores[0])
    c = dict(action='reserve', request_id='repeat', expected_version=s['version'], quote_id=quote(s, 'wave'))
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda n: stores[n % 2].command(s['id'], c), range(16)))
    current = stores[0].get(s['id'])
    if current['handoff_pending']:
        current = stores[1].operations.resume(s['id'], current['handoff']['id'])
    assert current['order'] and not current['handoff_pending']
    assert pg.snapshot(s['world_id'], current['order']['id'])['held_krw'] == 2800
    with pytest.raises(Conflict) as e:
        stores[1].command(s['id'], c | {'quote_id': 'changed'})
    assert e.value.code == 'IDEMPOTENCY_CONFLICT'


def test_decline_and_rejected_transfer_preserve_benefits(integrated):
    stores, _, pg = integrated
    a, b = stores
    refused = fresh(a, card_token='demo-declined')
    refused = cmd(b, refused, 'reserve', quote_id=quote(refused, 'wave'))
    assert refused['order'] is None and refused['wallet']['held_points'] == 0
    s = fresh(a)
    s = cmd(a, s, 'reserve', quote_id=quote(s, 'wave'))
    original_order, original_wallet = s['order'], s['wallet']
    s = a.transfer_control(s['id'], dict(action='fault', request_id='reject', expected_version=s['version'], fault='target_reject'))
    s = cmd(b, s, 'transfer', quote_id=quote(s, 'oat'))
    assert s['handoff']['status'] == 'REJECTED'
    assert s['order'] == original_order and s['wallet'] == original_wallet
    ledger = pg.snapshot(s['world_id'], s['order']['id'])
    assert ledger['held_krw'] == 2800 and ledger['capture_count'] == 0


@pytest.mark.parametrize('fault', ['authorize_reply_lost', 'capture_reply_lost', 'void_reply_lost'])
def test_other_instance_recovers_unknown_payment_without_new_key(integrated, fault):
    stores, _, pg = integrated
    a, b = stores
    s = fresh(a, payment_fault=fault)
    s = cmd(a, s, 'reserve', quote_id=quote(s, 'wave'))
    if fault != 'authorize_reply_lost':
        if fault == 'void_reply_lost':
            s = cmd(a, s, 'cancel')
        else:
            s = picked(a, s)
    assert s['handoff_pending']
    operation_id = s['handoff']['id']
    s = b.operations.resume(s['id'], operation_id)
    assert not s['handoff_pending']
    ledger = pg.snapshot(s['world_id'], s['order']['id'])
    assert ledger['capture_count'] == int(fault == 'capture_reply_lost')
    assert ledger['held_krw'] == (2800 if fault == 'authorize_reply_lost' else 0)


def test_merchant_capacity_is_shared_and_receipts_are_fenced(integrated):
    _, fleets, _ = integrated
    world = 'world-' + uuid4().hex
    def reserve(n):
        return fleets[n % 2].execute('oat', world=world, order_id='order-'+str(n), generation=0,
                                     operation_id='admit-'+str(n), action='ADMIT')
    with ThreadPoolExecutor(max_workers=8) as pool:
        replies = list(pool.map(reserve, range(16)))
    assert sum(r['ok'] for r in replies) == 1
    assert fleets[0].snapshot(world) == fleets[1].snapshot(world)
    assert fleets[0].snapshot(world)['oat']['used'] == 1
    assert fleets[1].snapshot('other')['oat']['used'] == 0
    winner = next(i for i, r in enumerate(replies) if r['ok'])
    assert reserve(winner) == replies[winner]
    conflict = fleets[1].execute('oat', world=world, order_id='different', generation=0,
                                operation_id='admit-'+str(winner), action='ADMIT')
    assert conflict == {'ok': False, 'code': 'COMMAND_CONFLICT'}


def test_merchant_abort_tombstone_rejects_late_hold(integrated):
    _, fleets, _ = integrated
    c = dict(world='w-'+uuid4().hex, order_id='order', generation=2, transfer_id='transfer')
    first = fleets[0].execute('oat', **c, operation_id='abort', action='ABORT_TARGET')
    assert first['ok']
    late = fleets[1].execute('oat', **c, operation_id='hold', action='HOLD')
    assert not late['ok'] and late['code'] == 'STALE_GENERATION'
    assert fleets[0].evidence(c['world'], c['order_id'])['merchants']['oat']['reservation']['phase'] == 'ABORTED'
