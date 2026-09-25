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
                    PRIMARY KEY(world, command_id));''')

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
                db.execute('COMMIT')
            except BaseException:
                if db.in_transaction:
                    db.execute('ROLLBACK')
                raise
        if self.after_commit:
            self.after_commit(shop, action, operation_id, result)
        return result

    def _apply(self, db, shop: str, row: dict | None, p: dict) -> dict:
        world, oid, gen, action, token = (p[k] for k in ('world', 'order_id', 'generation', 'action', 'transfer_id'))
        def failure(code):
            return dict(ok=False, code=code, phase=row['phase'] if row else None)
        if action in {'ADMIT', 'HOLD'}:
            policy = db.execute('SELECT accepting FROM policy WHERE world=?', (world,)).fetchone()
            if p['reject'] or (policy and not policy[0]):
                return failure('MERCHANT_REJECTED')
            if row and (gen <= row['generation'] or row['phase'] not in TERMINAL):
                return failure('STALE_GENERATION')
            if self._used(db, world) >= CAPACITY[shop]:
                return failure('CAPACITY_FULL')
            phase = 'RESERVED' if action == 'ADMIT' else 'HELD'
            db.execute('INSERT INTO seats VALUES(?,?,?,?,?) ON CONFLICT(world,order_id) DO UPDATE SET generation=excluded.generation,phase=excluded.phase,transfer_id=excluded.transfer_id',
                       (world, oid, gen, phase, token if action == 'HOLD' else ''))
        elif action == 'ABORT_TARGET' and (not row or (row['generation'] < gen and row['phase'] in TERMINAL)):
            # Tombstone blocks a delayed HOLD/ACTIVATE after compensation.
            db.execute('INSERT INTO seats VALUES(?,?,?,?,?) ON CONFLICT(world,order_id) DO UPDATE SET generation=excluded.generation,phase=excluded.phase,transfer_id=excluded.transfer_id',
                       (world, oid, gen, 'ABORTED', token))
        else:
            if not row or row['generation'] != gen:
                return failure('STALE_GENERATION')
            expected = {'FREEZE': ('RESERVED',), 'UNFREEZE': ('FROZEN',),
                        'RELEASE_SOURCE': ('FROZEN',), 'ACTIVATE': ('HELD',),
                        'ABORT_TARGET': ('HELD',), 'START': ('RESERVED',),
                        'READY': ('PREPARING',), 'CLAIM': ('READY', 'CLAIMED'),
                        'CANCEL': ('RESERVED', 'CANCELLED')}.get(action)
            if expected is None or row['phase'] not in expected:
                return failure('MERCHANT_STATE_CONFLICT')
            if action in {'UNFREEZE', 'RELEASE_SOURCE', 'ACTIVATE', 'ABORT_TARGET'} and row['transfer_id'] != token:
                return failure('TRANSFER_FENCE')
            next_phase = {'FREEZE': 'FROZEN', 'UNFREEZE': 'RESERVED', 'RELEASE_SOURCE': 'RELEASED',
                          'ACTIVATE': 'RESERVED', 'ABORT_TARGET': 'ABORTED', 'START': 'PREPARING',
                          'READY': 'READY', 'CLAIM': 'CLAIMED', 'CANCEL': 'CANCELLED'}[action]
            next_token = token if action in {'FREEZE', 'ABORT_TARGET', 'RELEASE_SOURCE'} else ''
            db.execute('UPDATE seats SET phase=?,transfer_id=? WHERE world=? AND order_id=?',
                       (next_phase, next_token, world, oid))
        return dict(ok=True, code='ACCEPTED', seat=self._row(db, world, oid))

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
