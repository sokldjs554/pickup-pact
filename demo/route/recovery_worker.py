"""Bounded recovery of persisted operations, independent of customer requests.

This worker never decides that silence means a merchant failed. Expiry can
compensate only a transfer with no durable COMMIT decision. Unresolved committed
work stays fenced and is escalated, not released on a timer.
"""
from __future__ import annotations
from copy import deepcopy
import json
import logging
import threading
import time

RETRY_BASE_SECONDS = 3.0
RETRY_CAP_SECONDS = 30.0
MAX_RETRY_FAILURES = 8
PREDECISION_SECONDS = 45.0
log = logging.getLogger(__name__)


def initial_schedule(now: float) -> dict:
    return dict(created_at=now, next_retry_at=now + RETRY_BASE_SECONDS,
                deadline_at=now + PREDECISION_SECONDS, retry_count=0,
                state='SCHEDULED', last_attempt_at=None)


def after_step(op: dict, now: float, paused: bool) -> None:
    recovery = op.setdefault('recovery', initial_schedule(now))
    recovery['last_attempt_at'] = now
    if op['status'] != 'PENDING':
        recovery.update(state='FINISHED', next_retry_at=None)
    elif paused:
        recovery['retry_count'] += 1
        delay = min(RETRY_BASE_SECONDS * 2 ** min(recovery['retry_count'] - 1, 6), RETRY_CAP_SECONDS)
        recovery['next_retry_at'] = now + delay
        if recovery['retry_count'] >= MAX_RETRY_FAILURES:
            recovery['state'] = 'REVIEW_REQUIRED'
            op['message'] = '매장 연결이 오래 끊겨 추가 확인이 필요해요. 주문을 취소하거나 새 결제로 처리하지 않았어요.'
        else:
            recovery['state'] = 'SCHEDULED'


def expire_undecided(op: dict, now: float) -> None:
    recovery = op.setdefault('recovery', initial_schedule(now))
    if (op['action'] != 'transfer' or op['decision'] != 'UNDECIDED'
            or now < recovery['deadline_at']):
        return
    op['decision'] = 'ABORT'
    op['failure'] = 'CONFIRMATION_EXPIRED'
    op['history'].append(dict(phase=op['phase'], label='변경 대기 시간 확인', result='ABORT_RECORDED'))
    # FREEZE may have reached the merchant without a reply. Reconfirm the same
    # phase first; then compensate. HOLD may also have committed: the target's
    # tombstone prevents a late HOLD/ACTIVATE from reviving it after compensation.
    if op['phase'] in {'HOLD', 'DECIDE'}:
        op['phase'] = 'ABORT_TARGET'
    op['message'] = '변경 확인이 늦어져 원래 주문을 유지할 수 있는지 확인하고 있어요.'


class RecoveryWorker:
    def __init__(self, store, interval: float = 1.0):
        if interval <= 0:
            raise ValueError('positive recovery interval required')
        self.store, self.interval = store, interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        with store.connection() as db:
            db.execute('''CREATE INDEX IF NOT EXISTS route_operations_due ON route_operations(
                json_extract(body,'$.status'), json_extract(body,'$.recovery.next_retry_at'))''')

    def run_once(self, now: float | None = None, limit: int = 16) -> dict:
        if not 1 <= limit <= 64:
            raise ValueError('recovery batch must be between 1 and 64')
        injected_now = now
        now = time.time() if now is None else now
        with self.store.connection() as db:
            rows = db.execute('''SELECT id,journey_id FROM route_operations
                WHERE json_extract(body,'$.status')='PENDING'
                AND COALESCE(json_extract(body,'$.recovery.state'),'SCHEDULED')!='REVIEW_REQUIRED'
                AND COALESCE(json_extract(body,'$.recovery.next_retry_at'),0)<=?
                ORDER BY COALESCE(json_extract(body,'$.recovery.next_retry_at'),0),id LIMIT ?''',
                (now, limit)).fetchall()
        result = dict(attempted=0, completed=0, pending=0, errors=0)
        for oid, sid in rows:
            if self._stop.is_set():
                break
            try:
                view = self.store.operations.resume(sid, oid, automatic=True, now=injected_now)
                result['attempted'] += 1
                result['pending' if view.get('handoff_pending') else 'completed'] += 1
            except Exception:
                result['errors'] += 1
                log.exception('recovery iteration failed for operation %s', oid)
        return result

    def _run(self):
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                # A broken iteration must not silently kill all future recovery.
                log.exception('recovery scan failed')
            self._stop.wait(self.interval)

    @property
    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def __enter__(self):
        if self.is_alive:
            raise RuntimeError('worker already running')
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name='pickup-recovery', daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *args):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=20)
            if self._thread.is_alive():
                raise RuntimeError('recovery worker did not stop within shutdown budget')
