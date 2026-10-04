"""Existing SQLite files tolerate concurrent lease-schema initialization."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from demo.route.payments.repository import PaymentRepository


def test_concurrent_open_migrates_pre_lease_outbox_once(tmp_path):
    import threading
    for attempt in range(5):
        path=tmp_path/f'old-{attempt}.sqlite'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE payment_outbox(event_id TEXT PRIMARY KEY,world_id TEXT NOT NULL,order_id TEXT NOT NULL,revision INTEGER NOT NULL,body TEXT NOT NULL,delivered INTEGER NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,next_at REAL NOT NULL,copies INTEGER NOT NULL DEFAULT 1)')
        barrier=threading.Barrier(8)
        def start(_):barrier.wait();return PaymentRepository(path).storage_ready()
        with ThreadPoolExecutor(max_workers=8) as pool:assert all(pool.map(start,range(8)))
