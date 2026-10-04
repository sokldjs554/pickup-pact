"""Independent durable stores for modeled merchants, not real merchant APIs.

One SQLite file per merchant. Capacity is shared by journeys within a demo
world. A held/frozen seat is occupied but cannot start manufacturing. Receipts
and generation fences make retries safe across coordinator process restarts.
"""
from __future__ import annotations
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from typing import Callable
from .planner import digest

CAPACITY = {'wave': 4, 'corner': 4, 'oat': 1, 'garden': 1, 'express': 1}
OCCUPIED = ('RESERVED', 'FROZEN', 'HELD', 'PREPARING', 'READY')
TERMINAL = ('RELEASED', 'ABORTED', 'CANCELLED', 'CLAIMED')


class MerchantFleet:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.after_commit: Callable | None = None  # Test hook after a real commit.
        for shop in CAPACITY:
            with self.connection(shop) as db:
                db.executescript('''PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS seats(
                    world TEXT NOT NULL, order_id TEXT NOT NULL, generation INTEGER NOT NULL,
                    phase TEXT NOT NULL, transfer_id TEXT NOT NULL,
                    PRIMARY KEY(world, order_id));
                CREATE INDEX IF NOT EXISTS seats_capacity ON seats(world, phase);
                CREATE TABLE IF NOT EXISTS policy(world TEXT PRIMARY KEY, accepting INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS receipts(
                    world TEXT NOT NULL, command_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, result TEXT NOT NULL,
                    PRIMARY KEY(world, command_id));
                CREATE TABLE IF NOT EXISTS receipt_context(
                    world TEXT NOT NULL, command_id TEXT NOT NULL, order_id TEXT NOT NULL,
                    action TEXT NOT NULL, generation INTEGER NOT NULL,
                    PRIMARY KEY(world,command_id));''')

    @contextmanager
    def connection(self, shop: str):
        if shop not in CAPACITY:
            raise ValueError('unknown modeled merchant')
        db = sqlite3.connect(self.directory / f'{shop}.sqlite', timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA synchronous=FULL')
            yield db
        finally:
            db.close()

    @staticmethod
    def _row(db, world: str, order_id: str) -> dict | None:
        row = db.execute('SELECT * FROM seats WHERE world=? AND order_id=?', (world, order_id)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _used(db, world: str) -> int:
        return db.execute("SELECT count(*) FROM seats WHERE world=? AND phase IN ('RESERVED','FROZEN','HELD','PREPARING','READY')", (world,)).fetchone()[0]

    def execute(self, shop: str, *, world: str, order_id: str, generation: int,
                operation_id: str, action: str, transfer_id: str = '', reject: bool = False) -> dict:
        payload = dict(world=world, order_id=order_id, generation=generation,
                       action=action, transfer_id=transfer_id, reject=reject)
        fp = digest(payload)
        with self.connection(shop) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                previous = db.execute('SELECT fingerprint,result FROM receipts WHERE world=? AND command_id=?',
                                      (world, operation_id)).fetchone()
                if previous:
                    if previous['fingerprint'] != fp:
                        result = dict(ok=False, code='COMMAND_CONFLICT')
                    else:
                        result = json.loads(previous['result'])
                    db.execute('COMMIT')
                    return result
                row = self._row(db, world, order_id)
                result = self._apply(db, shop, row, payload)
                db.execute('INSERT INTO receipts VALUES(?,?,?,?)',
                           (world, operation_id, fp, json.dumps(result, sort_keys=True)))
                db.execute('INSERT INTO receipt_context VALUES(?,?,?,?,?)',
                           (world,operation_id,order_id,action,generation))
                db.execute('COMMIT')
            except BaseException:
                if db.in_transaction:
                    db.execute('ROLLBACK')
                raise
        if self.after_commit:
            self.after_commit(shop, action, operation_id, result)
        return result

    def _apply(self, db, shop: str, row: dict | None, p: dict) -> dict:
        from .merchant_rules import transition
        policy = db.execute('SELECT accepting FROM policy WHERE world=?', (p['world'],)).fetchone()
        result = transition(row, p, accepting=not policy or bool(policy[0]),
                            used=self._used(db, p['world']), capacity=CAPACITY[shop])
        if result['ok']:
            seat = result['seat']
            db.execute('INSERT INTO seats VALUES(?,?,?,?,?) ON CONFLICT(world,order_id) DO UPDATE SET generation=excluded.generation,phase=excluded.phase,transfer_id=excluded.transfer_id',
                       tuple(seat[k] for k in ('world','order_id','generation','phase','transfer_id')))
        return result

    def set_accepting(self, shop: str, world: str, accepting: bool) -> None:
        """Modeled merchant control used by bounded same-input experiments."""
        with self.connection(shop) as db:
            db.execute('INSERT INTO policy VALUES(?,?) ON CONFLICT(world) DO UPDATE SET accepting=excluded.accepting',
                       (world, int(accepting)))

    def snapshot(self, world: str) -> dict:
        result = {}
        for shop, limit in CAPACITY.items():
            with self.connection(shop) as db:
                used = self._used(db, world)
                rows = db.execute('SELECT order_id,generation,phase,transfer_id FROM seats WHERE world=? ORDER BY order_id', (world,)).fetchall()
                result[shop] = dict(capacity=limit, used=used, available=limit-used,
                                    reservations=[dict(row) for row in rows])
        return result

    def evidence(self, world: str, order_id: str) -> dict:
        """Read each independent DB atomically. World receipt rowids fence ABA changes."""
        out={}
        for shop in CAPACITY:
            with self.connection(shop) as db:
                db.execute('BEGIN')
                row=self._row(db,world,order_id)
                revision=db.execute('SELECT COALESCE(MAX(rowid),0) FROM receipts WHERE world=?',(world,)).fetchone()[0]
                last=db.execute("""SELECT r.command_id,r.result,c.action,c.generation
                    FROM receipts r JOIN receipt_context c ON r.world=c.world AND r.command_id=c.command_id
                    WHERE r.world=? AND c.order_id=? ORDER BY r.rowid DESC LIMIT 1""",(world,order_id)).fetchone()
                receipt=dict(command_id=last['command_id'],action=last['action'],generation=last['generation'],
                             result=json.loads(last['result'])) if last else None
                db.execute('COMMIT')
                out[shop]=dict(reservation=row,revision=revision,last_receipt=receipt)
        return dict(mode='synthetic',world_id=world,order_id=order_id,merchants=out)
