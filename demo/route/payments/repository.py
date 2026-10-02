"""Financial state, immutable receipts and outbox committed in one SQLite DB."""
from __future__ import annotations
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4
from .domain import PaymentError, bound, canonical, fingerprint, key, validate


class PaymentRepository:
    transport = 'local_test'

    def __init__(self, path: str | Path, limit_krw: int = 100000):
        if type(limit_krw) is not int or limit_krw < 1:
            raise ValueError('positive synthetic credit limit required')
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS payment_settings(name TEXT PRIMARY KEY,value INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS payment_authorizations(
                    authorization_id TEXT PRIMARY KEY,world_id TEXT NOT NULL,order_id TEXT NOT NULL,
                    amount_krw INTEGER NOT NULL CHECK(amount_krw>0),currency TEXT NOT NULL CHECK(currency='KRW'),
                    payment_revision INTEGER NOT NULL,quote_fingerprint TEXT NOT NULL,card_token TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('AUTHORIZED','CAPTURED','VOIDED')),
                    created_at REAL NOT NULL,updated_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS payment_order ON payment_authorizations(world_id,order_id);
                CREATE TABLE IF NOT EXISTS payment_commands(
                    operation_key TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,result TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS payment_transactions(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,
                    authorization_id TEXT NOT NULL REFERENCES payment_authorizations(authorization_id),
                    world_id TEXT NOT NULL,order_id TEXT NOT NULL,kind TEXT NOT NULL,
                    amount_krw INTEGER NOT NULL,at REAL NOT NULL,
                    UNIQUE(authorization_id,kind));
                CREATE UNIQUE INDEX IF NOT EXISTS payment_one_capture_per_order
                    ON payment_transactions(world_id,order_id) WHERE kind='CAPTURE';
                CREATE INDEX IF NOT EXISTS payment_tx_order ON payment_transactions(world_id,order_id,seq);
                CREATE TABLE IF NOT EXISTS payment_outbox(
                    event_id TEXT PRIMARY KEY,world_id TEXT NOT NULL,order_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,body TEXT NOT NULL,delivered INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,next_at REAL NOT NULL,copies INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS payment_faults(
                    world_id TEXT NOT NULL,operation_key TEXT NOT NULL,mode TEXT NOT NULL,
                    PRIMARY KEY(world_id,operation_key,mode));
            ''')
            # Existing SQLite files remain valid; the new fields only fence
            # notification ownership, never rewrite original financial records.
            columns={r[1] for r in db.execute('PRAGMA table_info(payment_outbox)')}
            for name,ddl in [('lease_owner','TEXT'),('lease_version','INTEGER NOT NULL DEFAULT 0'),('lease_until','REAL')]:
                if name not in columns: db.execute(f'ALTER TABLE payment_outbox ADD COLUMN {name} {ddl}')
            db.execute('INSERT OR IGNORE INTO payment_settings VALUES(?,?)', ('limit_krw', limit_krw))
            self.limit_krw = db.execute("SELECT value FROM payment_settings WHERE name='limit_krw'").fetchone()[0]

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA synchronous=FULL')
        try:
            yield db
        finally:
            db.close()

    @staticmethod
    def _response(c: dict, outcome: str, transaction_id: str | None, **extra) -> dict:
        result = {k: v for k, v in c.items() if k != 'card_token'}
        return result | dict(ok=True, outcome=outcome, transaction_id=transaction_id,
                             provider='DEMO_PLATFORM', mode='synthetic') | extra

    def execute(self, command: dict, *, notification_copies: int = 1, notification_delay: float = 0) -> dict:
        c = validate(command)
        if notification_copies not in {1,2} or not 0<=notification_delay<=3:
            raise ValueError('bounded notification options required')
        delivery=(notification_copies, notification_delay)
        fp = fingerprint(c)
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                old = db.execute('SELECT fingerprint,result FROM payment_commands WHERE operation_key=?',
                                 (c['operation_key'],)).fetchone()
                if old:
                    if old['fingerprint'] != fp:
                        raise PaymentError('IDEMPOTENCY_CONFLICT')
                    db.execute('COMMIT')
                    return json.loads(old['result'])
                result = self._apply(db, c, delivery)
                db.execute('INSERT INTO payment_commands VALUES(?,?,?)',
                           (c['operation_key'], fp, canonical(result)))
                db.execute('COMMIT')
                return result
            except BaseException:
                if db.in_transaction:
                    db.execute('ROLLBACK')
                raise

    def _apply(self, db, c, delivery):
        row = db.execute('SELECT * FROM payment_authorizations WHERE authorization_id=?',
                         (c['authorization_id'],)).fetchone()
        action = c['action']
        if row:
            bound(dict(row), c)
        if action == 'AUTHORIZE':
            captured = db.execute("SELECT id FROM payment_transactions WHERE world_id=? AND order_id=? AND kind='CAPTURE'",
                                  (c['world_id'], c['order_id'])).fetchone()
            if captured:
                raise PaymentError('ORDER_ALREADY_CAPTURED')
            if row:
                if row['status'] != 'AUTHORIZED' or row['card_token'] != c['card_token']:
                    raise PaymentError('AUTHORIZATION_NOT_ACTIVE')
                return self._existing_receipt(db, c, 'AUTHORIZED', 'AUTHORIZE')
            if c['card_token'] == 'demo-declined':
                return self._response(c, 'DECLINED', None, ok=False, code='DECLINED')
            used = db.execute("SELECT COALESCE(SUM(amount_krw),0) FROM payment_authorizations WHERE world_id=? AND card_token=? AND status IN ('AUTHORIZED','CAPTURED')",
                              (c['world_id'], c['card_token'])).fetchone()[0]
            if used + c['amount_krw'] > self.limit_krw:
                return self._response(c, 'DECLINED', None, ok=False, code='LIMIT_EXCEEDED')
            now = time.time()
            db.execute('INSERT INTO payment_authorizations VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (c['authorization_id'],c['world_id'],c['order_id'],c['amount_krw'],c['currency'],
                        c['payment_revision'],c['quote_fingerprint'],c['card_token'],'AUTHORIZED',now,now))
            return self._record(db, c, 'AUTHORIZED', delivery)
        if not row:
            raise PaymentError('AUTHORIZATION_NOT_FOUND')
        outcome = 'CAPTURED' if action == 'CAPTURE' else 'VOIDED'
        if row['status'] == outcome:
            return self._existing_receipt(db, c, outcome, action)
        if row['status'] != 'AUTHORIZED':
            raise PaymentError('AUTHORIZATION_NOT_ACTIVE')
        if action == 'CAPTURE':
            if db.execute("SELECT 1 FROM payment_transactions WHERE world_id=? AND order_id=? AND kind='CAPTURE'",
                          (c['world_id'], c['order_id'])).fetchone():
                raise PaymentError('ORDER_ALREADY_CAPTURED')
        db.execute('UPDATE payment_authorizations SET status=?,updated_at=? WHERE authorization_id=?',
                   (outcome, time.time(), c['authorization_id']))
        return self._record(db, c, outcome, delivery)

    def _existing_receipt(self, db, c, outcome, kind):
        row = db.execute('SELECT id,seq FROM payment_transactions WHERE authorization_id=? AND kind=?',
                         (c['authorization_id'], kind)).fetchone()
        if not row:
            raise PaymentError('LEDGER_INCONSISTENT')
        return self._response(c, outcome, row['id'], revision=row['seq'])

    def _record(self, db, c, outcome, delivery):
        txid, now = 'TX-'+uuid4().hex, time.time()
        cursor = db.execute('INSERT INTO payment_transactions(id,authorization_id,world_id,order_id,kind,amount_krw,at) VALUES(?,?,?,?,?,?,?)',
                            (txid,c['authorization_id'],c['world_id'],c['order_id'],c['action'],c['amount_krw'],now))
        revision = cursor.lastrowid
        event_id = 'EV-'+uuid4().hex
        event = dict(event_id=event_id,world_id=c['world_id'],order_id=c['order_id'],
                     authorization_id=c['authorization_id'],transaction_id=txid,
                     revision=revision,kind=c['action'],payment_revision=c['payment_revision'],
                     amount_krw=c['amount_krw'],currency='KRW',mode='synthetic')
        db.execute('INSERT INTO payment_outbox(event_id,world_id,order_id,revision,body,next_at,copies) VALUES(?,?,?,?,?,?,?)',
                   (event_id,c['world_id'],c['order_id'],revision,canonical(event),now+delivery[1],delivery[0]))
        return self._response(c, outcome, txid, revision=revision)

    def operation(self, operation_key: str) -> dict | None:
        with self.connection() as db:
            row = db.execute('SELECT result FROM payment_commands WHERE operation_key=?', (key(operation_key),)).fetchone()
            return json.loads(row['result']) if row else None

    def snapshot(self, world: str, order_id: str) -> dict:
        key(world); key(order_id)
        with self.connection() as db:
            db.execute('BEGIN')
            auths = [dict(r) for r in db.execute('SELECT * FROM payment_authorizations WHERE world_id=? AND order_id=? ORDER BY payment_revision,authorization_id', (world,order_id))]
            txs = [dict(r) for r in db.execute('SELECT * FROM payment_transactions WHERE world_id=? AND order_id=? ORDER BY seq', (world,order_id))]
            db.execute('COMMIT')
        for a in auths:
            a.pop('card_token')
        return dict(mode='synthetic',provider='DEMO_PLATFORM',world_id=world,order_id=order_id,
                    currency='KRW',revision=max((t['seq'] for t in txs),default=0),
                    authorizations=auths,transactions=txs,
                    held_krw=sum(a['amount_krw'] for a in auths if a['status']=='AUTHORIZED'),
                    captured_krw=sum(t['amount_krw'] for t in txs if t['kind']=='CAPTURE'),
                    capture_count=sum(t['kind']=='CAPTURE' for t in txs))

    def consume_fault(self, world: str, operation_key: str, mode: str) -> bool:
        if mode not in {'drop_reply','duplicate_notification','late_notification'}:
            return False
        with self.connection() as db:
            cur = db.execute('INSERT OR IGNORE INTO payment_faults VALUES(?,?,?)', (key(world),key(operation_key),mode))
            return cur.rowcount == 1


    def storage_ready(self) -> bool:
        with self.connection() as db:
            return db.execute("SELECT value FROM payment_settings WHERE name='limit_krw'").fetchone() is not None

    def claim_notifications(self,worker: str,*,limit: int=16,lease_seconds: int=30) -> list[dict]:
        key(worker)
        if type(limit) is not int or not 1<=limit<=64 or type(lease_seconds) is not int or not 1<=lease_seconds<=120:
            raise ValueError('bounded outbox lease required')
        now=time.time()
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            rows=[dict(r) for r in db.execute("SELECT * FROM payment_outbox WHERE delivered=0 AND attempts<8 AND next_at<=? AND (lease_until IS NULL OR lease_until<=?) ORDER BY revision LIMIT ?",(now,now,limit))]
            for row in rows:
                row.update(lease_owner=worker,lease_version=row['lease_version']+1,lease_until=now+lease_seconds)
                db.execute('UPDATE payment_outbox SET lease_owner=?,lease_version=?,lease_until=? WHERE event_id=?',
                           (worker,row['lease_version'],row['lease_until'],row['event_id']))
            db.execute('COMMIT')
        return rows

    def finish_notification(self,claim: dict,ok: bool) -> bool:
        if type(ok) is not bool: raise ValueError('explicit delivery outcome required')
        now=time.time()
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM payment_outbox WHERE event_id=? AND lease_owner=? AND lease_version=? AND delivered=0 AND lease_until>?',
                (claim['event_id'],claim['lease_owner'],claim['lease_version'],now)).fetchone()
            if not row:
                db.execute('COMMIT');return False
            delay=min(3*2**min(row['attempts'],4),30)
            db.execute('UPDATE payment_outbox SET delivered=?,attempts=attempts+1,next_at=?,lease_owner=NULL,lease_until=NULL WHERE event_id=?',
                (int(ok),now+delay,claim['event_id']))
            db.execute('COMMIT')
            return True
