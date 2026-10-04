"""Native merchant data owner; capacity and immutable receipts share one commit."""
from pathlib import Path
from ..merchant_fleet import CAPACITY, OCCUPIED
from ..merchant_rules import transition
from ..payments.domain import key
from ..planner import digest
from .database import PostgresDatabase, transaction_lock


class PostgresMerchantFleet:
    backend = 'postgresql'

    def __init__(self, dsn, *, schema='pact_merchants', initialize=True, runtime_guard=None):
        if initialize and runtime_guard is not None:
            raise ValueError('ha_postgres_v1 runtime users never run DDL; use the owner migration')
        self.database = PostgresDatabase(dsn, schema=schema, runtime_guard=runtime_guard)
        try:
            if initialize:
                self.database.initialize((Path(__file__).parent / 'sql/merchants.sql').read_text())
        except Exception:
            self.close()
            raise

    def close(self):
        self.database.close()

    @staticmethod
    def _shop(shop):
        if shop not in CAPACITY:
            raise ValueError('unknown modeled merchant')
        return shop

    @staticmethod
    def _row(db, shop, world, oid):
        return db.execute('SELECT world,order_id,generation,phase,transfer_id FROM merchant_seats WHERE shop=%s AND world=%s AND order_id=%s',
                          (shop, world, oid)).fetchone()

    @staticmethod
    def _used(db, shop, world):
        return db.execute('SELECT count(*) AS n FROM merchant_seats WHERE shop=%s AND world=%s AND phase=ANY(%s)',
                          (shop, world, list(OCCUPIED))).fetchone()['n']

    def execute(self, shop, *, world, order_id, generation, operation_id, action, transfer_id='', reject=False):
        self._shop(shop)
        for value in (world, order_id, operation_id):
            key(value)
        if transfer_id:
            key(transfer_id)
        if type(generation) is not int or not 0 <= generation <= 1_000_000_000 or type(reject) is not bool:
            raise ValueError('invalid merchant command')
        from ..merchant_http import ACTIONS
        if action not in ACTIONS:
            raise ValueError('invalid merchant action')
        payload = dict(world=world, order_id=order_id, generation=generation,
                       action=action, transfer_id=transfer_id, reject=reject)
        fp = digest(payload)
        from psycopg.types.json import Jsonb
        with self.database.transaction() as db:
            # Capacity is per merchant/world. Different worlds never need this lock.
            transaction_lock(db, 'merchant-capacity', digest([self.database.schema, shop, world]))
            old = db.execute('SELECT fingerprint,result FROM merchant_receipts WHERE shop=%s AND world=%s AND command_id=%s',
                             (shop, world, operation_id)).fetchone()
            if old:
                return old['result'] if old['fingerprint'] == fp else dict(ok=False, code='COMMAND_CONFLICT')
            policy = db.execute('SELECT accepting FROM merchant_policy WHERE shop=%s AND world=%s', (shop, world)).fetchone()
            result = transition(self._row(db, shop, world, order_id), payload,
                                accepting=not policy or policy['accepting'],
                                used=self._used(db, shop, world), capacity=CAPACITY[shop])
            if result['ok']:
                seat = result['seat']
                db.execute('''INSERT INTO merchant_seats(shop,world,order_id,generation,phase,transfer_id)
                    VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(shop,world,order_id)
                    DO UPDATE SET generation=excluded.generation,phase=excluded.phase,transfer_id=excluded.transfer_id''',
                           (shop, world, order_id, seat['generation'], seat['phase'], seat['transfer_id']))
            db.execute('''INSERT INTO merchant_receipts(shop,world,command_id,fingerprint,result,order_id,action,generation)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s)''',
                       (shop, world, operation_id, fp, Jsonb(result), order_id, action, generation))
            return result

    def set_accepting(self, shop, world, accepting):
        self._shop(shop)
        key(world)
        if type(accepting) is not bool:
            raise ValueError('boolean policy required')
        with self.database.transaction() as db:
            transaction_lock(db, 'merchant-capacity', digest([self.database.schema, shop, world]))
            db.execute('''INSERT INTO merchant_policy VALUES(%s,%s,%s) ON CONFLICT(shop,world)
                DO UPDATE SET accepting=excluded.accepting''', (shop, world, accepting))

    def snapshot(self, world):
        key(world)
        out = {}
        with self.database.transaction(readonly=True) as db:
            for shop, capacity in CAPACITY.items():
                rows = db.execute('SELECT order_id,generation,phase,transfer_id FROM merchant_seats WHERE shop=%s AND world=%s ORDER BY order_id',
                                  (shop, world)).fetchall()
                used = sum(r['phase'] in OCCUPIED for r in rows)
                out[shop] = dict(capacity=capacity, used=used, available=capacity-used, reservations=rows)
        return out

    def evidence(self, world, order_id):
        key(world)
        key(order_id)
        out = {}
        with self.database.transaction(readonly=True) as db:
            for shop in CAPACITY:
                revision = db.execute('SELECT COALESCE(MAX(seq),0) AS n FROM merchant_receipts WHERE shop=%s AND world=%s',
                                      (shop, world)).fetchone()['n']
                last = db.execute('''SELECT command_id,result,action,generation FROM merchant_receipts
                    WHERE shop=%s AND world=%s AND order_id=%s ORDER BY seq DESC LIMIT 1''',
                                  (shop, world, order_id)).fetchone()
                out[shop] = dict(reservation=self._row(db, shop, world, order_id), revision=revision, last_receipt=last)
        return dict(mode='synthetic', world_id=world, order_id=order_id, merchants=out)

    def consume_reply_loss(self, world, operation_id):
        key(world)
        key(operation_id)
        with self.database.transaction() as db:
            return bool(db.execute('INSERT INTO merchant_reply_loss VALUES(%s,%s) ON CONFLICT DO NOTHING RETURNING command_id',
                                   (world, operation_id)).fetchone())

    def storage_ready(self):
        with self.database.transaction(readonly=True) as db:
            return db.execute('SELECT count(*) AS n FROM merchant_seats').fetchone()['n'] >= 0
