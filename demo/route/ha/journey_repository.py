"""Native journey unit of work: state, request, intent and references commit together."""
from contextlib import contextmanager
from pathlib import Path
from ..payments.domain import key, PaymentError
from .database import PostgresDatabase, transaction_lock
from .operation_store import PostgresOperationStore


class PostgresJourneyRepository:
    def __init__(self, dsn, *, schema='pact_orders', initialize=True, runtime_guard=None):
        if initialize and runtime_guard is not None:
            raise ValueError('ha_postgres_v1 runtime users never run DDL; use the owner migration')
        self.database = PostgresDatabase(dsn, schema=schema, runtime_guard=runtime_guard)
        try:
            if initialize:
                directory = Path(__file__).parent / 'sql'
                self.database.initialize((directory/'operations.sql').read_text() + (directory/'orders.sql').read_text())
        except Exception:
            self.close()
            raise

    def close(self):
        self.database.close()

    def create(self, state):
        from psycopg.types.json import Jsonb
        with self.database.transaction() as db:
            db.execute('INSERT INTO journeys VALUES(%s,%s,%s)', (state['id'], state['version'], Jsonb(state)))

    def read(self, sid):
        with self.database.transaction(readonly=True) as db:
            row = db.execute('SELECT body FROM journeys WHERE id=%s', (key(sid),)).fetchone()
            if row is None:
                raise KeyError(sid)
            return row['body']

    @contextmanager
    def unit(self, sid):
        with self.database.transaction() as db:
            transaction_lock(db, 'journey', self.database.schema + ':' + key(sid))
            row = db.execute('SELECT body FROM journeys WHERE id=%s FOR UPDATE', (sid,)).fetchone()
            if row is None:
                raise KeyError(sid)
            yield JourneyUnit(db, row['body'])

    def due(self, limit=16):
        if type(limit) is not int or not 1 <= limit <= 64:
            raise ValueError('bounded recovery batch required')
        with self.database.transaction(readonly=True) as db:
            return db.execute("""SELECT id,order_id AS sid FROM ha_operations
                WHERE status='PENDING' AND next_at<=clock_timestamp()
                  AND payload->'recovery'->>'state'<>'REVIEW_REQUIRED'
                  AND (lease_until IS NULL OR lease_until<=clock_timestamp())
                ORDER BY next_at,id LIMIT %s""", (limit,)).fetchall()

    def heartbeat(self, node_id, remove=False):
        with self.database.transaction() as db:
            if remove:
                db.execute('DELETE FROM recovery_nodes WHERE node_id=%s', (key(node_id),))
            else:
                db.execute('INSERT INTO recovery_nodes VALUES(%s,clock_timestamp()) ON CONFLICT(node_id) DO UPDATE SET seen_at=excluded.seen_at', (key(node_id),))

    def worker_ready(self):
        with self.database.transaction(readonly=True) as db:
            return bool(db.execute("SELECT 1 FROM recovery_nodes WHERE seen_at>clock_timestamp()-interval '15 seconds' LIMIT 1").fetchone())

    def storage_ready(self):
        with self.database.transaction(readonly=True) as db:
            return db.execute('SELECT count(*) AS n FROM journeys').fetchone()['n'] >= 0


class JourneyUnit:
    def __init__(self, db, state):
        self.db, self.state = db, state

    def now(self):
        return float(self.db.execute('SELECT EXTRACT(EPOCH FROM clock_timestamp()) AS t').fetchone()['t'])

    def request(self, channel, rid):
        return self.db.execute('SELECT fingerprint,operation_id FROM journey_requests WHERE journey_id=%s AND channel=%s AND request_id=%s',
                               (self.state['id'], channel, rid)).fetchone()

    def record(self, channel, rid, fingerprint, operation_id=None):
        self.db.execute('INSERT INTO journey_requests VALUES(%s,%s,%s,%s,%s)',
                        (self.state['id'], channel, rid, fingerprint, operation_id))

    def save(self, state):
        from psycopg.types.json import Jsonb
        if state['id'] != self.state['id']:
            raise ValueError('journey identity cannot change')
        self.db.execute('UPDATE journeys SET version=%s,body=%s WHERE id=%s',
                        (state['version'], Jsonb(state), state['id']))

    def operation(self, oid):
        row = self.db.execute('''SELECT *,lease_until>clock_timestamp() AS leased,
                next_at>clock_timestamp() AS not_due FROM ha_operations WHERE id=%s FOR UPDATE''', (oid,)).fetchone()
        if row is None or row['order_id'] != self.state['id'] or row['payload'].get('sid') != self.state['id']:
            raise KeyError(oid)
        return row

    def enqueue(self, op):
        # The queue aggregate is a journey; the business order ID remains in payload.
        PostgresOperationStore.enqueue_in(self.db, op['id'], op['sid'], op, request_key=op['request_id'])
        self.schedule(op)

    def schedule(self, op):
        due = op['recovery']['next_retry_at']
        if op['status'] != 'PENDING' or op['recovery']['state'] == 'REVIEW_REQUIRED':
            self.db.execute("UPDATE ha_operations SET next_at=clock_timestamp() WHERE id=%s", (op['id'],))
        else:
            self.db.execute('UPDATE ha_operations SET next_at=to_timestamp(%s) WHERE id=%s', (due, op['id']))

    def replace_unleased(self, op):
        from psycopg.types.json import Jsonb
        row = self.db.execute('''UPDATE ha_operations SET payload=%s,phase_version=%s
            WHERE id=%s AND (lease_until IS NULL OR lease_until<=clock_timestamp()) RETURNING id''',
                              (Jsonb(op), op['phase_version'], op['id'])).fetchone()
        if not row:
            return False
        self.schedule(op)
        return True

    def claim(self, oid, worker, lease_seconds, automatic):
        return PostgresOperationStore.claim_one_in(self.db, oid, worker, lease_seconds=lease_seconds, force=not automatic)

    def apply(self, claim, op, state):
        applied = PostgresOperationStore.apply_in(self.db, claim, op, finished=op['status'] != 'PENDING')
        if applied:
            self.save(state)
            self.schedule(op)
        return applied

    def register(self, auth):
        for name in ('world_id', 'order_id', 'authorization_id'):
            key(auth[name])
        transaction_lock(self.db, 'notification-binding', auth['authorization_id'])
        row = self.db.execute('SELECT world_id,order_id FROM authorization_refs WHERE authorization_id=%s',
                              (auth['authorization_id'],)).fetchone()
        expected = dict(world_id=auth['world_id'], order_id=auth['order_id'])
        if row and row != expected:
            raise PaymentError('AUTHORIZATION_BINDING_CONFLICT')
        self.db.execute('INSERT INTO authorization_refs VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',
                        (auth['authorization_id'], auth['world_id'], auth['order_id']))
