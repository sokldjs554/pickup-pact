"""Resumable application operations over separately committed merchant DBs.

No distributed transaction or live merchant integration is claimed.
The public runtime uses a separate synthetic merchant process over HTTP. A process
may die after any merchant commit and before acknowledgement: the durable
intent and stable phase keys make the next recovery continue the same work.
"""
from __future__ import annotations
from copy import deepcopy
import json
import sqlite3
import time
from .recovery_worker import initial_schedule, after_step, expire_undecided
from uuid import uuid4
from .planner import digest, plans

ACTIONS = {'reserve', 'reorder', 'transfer', 'start', 'ready', 'claim', 'cancel'}
FAULTS = {'none', 'target_reject', 'after_target_hold', 'after_source_release', 'after_target_activation'}
PHASE_TEXT = {
    'FREEZE': '원래 매장에 제조를 잠시 멈춰 달라고 요청해요',
    'HOLD': '새 매장이 주문을 받을 수 있는지 확인해요',
    'DECIDE': '새 매장의 자리를 확보했어요',
    'RELEASE_SOURCE': '원래 매장의 자리를 정리해요',
    'ACTIVATE': '새 매장에서 주문을 이어받아요',
    'FINALIZE': '주문과 혜택을 같은 결과로 맞춰요',
    'ABORT_TARGET': '새 매장에 보류한 자리를 돌려줘요',
    'UNFREEZE': '원래 매장의 주문을 다시 이어가요',
    'ADMIT': '매장이 주문을 받을 수 있는지 확인해요',
    'GUEST_ADMIT': '체험 손님의 자리를 확인해요',
    'GUEST_CANCEL': '체험 손님의 자리를 정리해요',
    'START': '매장에서 커피 만들기를 시작해요', 'READY': '준비 상태를 확인해요',
    'CLAIM': '수령과 혜택 사용을 확인해요', 'CANCEL': '주문과 혜택을 취소해요',
}
ERROR_TEXT = {
    'CONFIRMATION_EXPIRED': '매장 변경 확인이 늦어져 원래 주문과 혜택을 유지했어요.',
    'CAPACITY_FULL': '다른 손님이 마지막 자리를 먼저 잡았어요. 원래 주문과 혜택은 그대로예요.',
    'MERCHANT_REJECTED': '새 매장이 주문을 받지 못했어요. 원래 주문과 혜택은 그대로예요.',
    'MERCHANT_STATE_CONFLICT': '매장의 처리 상태가 달라 확인이 필요해요.',
    'STALE_GENERATION': '이전 주문 상태의 요청이라 처리하지 않았어요.',
    'TRANSFER_FENCE': '다른 매장 변경 작업의 요청이라 처리하지 않았어요.',
}


def public_operation(op: dict) -> dict:
    return {key: deepcopy(op[key]) for key in ('id', 'action', 'phase', 'status', 'decision',
            'message', 'failure', 'history', 'source', 'target', 'attempts')} | {
                'recovery': deepcopy(op.get('recovery', {}))}


class DurableOperations:
    def __init__(self, store):
        self.store = store
        with store.connection() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS route_operations(
                id TEXT PRIMARY KEY, journey_id TEXT NOT NULL, request_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL, body TEXT NOT NULL,
                UNIQUE(journey_id,request_id));''')

    def _op(self, db, oid):
        row = db.execute('SELECT body FROM route_operations WHERE id=?', (oid,)).fetchone()
        if row is None:
            raise KeyError(oid)
        return json.loads(row[0])

    @staticmethod
    def _save_op(db, op):
        db.execute('UPDATE route_operations SET body=? WHERE id=?', (json.dumps(op, ensure_ascii=False), op['id']))

    def command(self, sid: str, c: dict):
        from .store import require
        fp = digest(c)
        # Every external side effect has a durable candidate/intention first.
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                s = self.store._load(db, sid)
                if 'world_id' not in s:
                    db.execute('COMMIT')
                    return None  # Explicit legacy path, never silently migrated.
                previous = db.execute('SELECT id,fingerprint FROM route_operations WHERE journey_id=? AND request_id=?',
                                      (sid, c['request_id'])).fetchone()
                if previous:
                    require(previous[1] == fp, 'IDEMPOTENCY_CONFLICT', '이미 보낸 요청과 내용이 달라요.')
                    oid = previous[0]
                    db.execute('COMMIT')
                    return self.resume(sid, oid, duplicate=True)
                previous_local = db.execute('SELECT fingerprint FROM route_commands WHERE journey_id=? AND request_id=?',
                                            (sid, c['request_id'])).fetchone()
                if previous_local:
                    require(previous_local[0] == fp, 'IDEMPOTENCY_CONFLICT', '이미 보낸 요청과 내용이 달라요.')
                    db.execute('COMMIT')
                    return self.store.view(s, True)
                require(s['version'] == c['expected_version'], 'STALE_VERSION', '주문 상태를 다시 확인해 주세요.')
                if c['action'] == 'recover':
                    oid = s.get('active_operation')
                    db.execute('COMMIT')
                    return self.resume(sid, oid) if oid else self.store.view(s)
                require(not s.get('active_operation'), 'TRANSFER_PENDING', '매장 확인이 끝나지 않았어요. 먼저 다시 확인해 주세요.')
                if c['action'] not in ACTIONS:
                    db.execute('COMMIT')
                    return None
                candidate = deepcopy(s)
                self.store._apply(candidate, c)  # Validate pricing/domain before external changes.
                before_order, after_order = s.get('order'), candidate['order']
                oid = uuid4().hex
                gen = (before_order or {}).get('merchant_generation', 0)
                if c['action'] == 'transfer':
                    # A rejected attempt leaves a target tombstone. Allocate a
                    # fresh generation for every attempt, not only successful
                    # transfers, so that delayed requests remain fenced without
                    # preventing a later legitimate retry.
                    gen = max(gen, s.get('merchant_generation_counter', 0)) + 1
                    s['merchant_generation_counter'] = gen
                    candidate['merchant_generation_counter'] = gen
                elif c['action'] in {'reserve', 'reorder'}:
                    gen = 0
                after_order['merchant_generation'] = gen
                fault = s.get('next_transfer_fault', 'none') if c['action'] == 'transfer' else 'none'
                phase = 'FREEZE' if c['action'] == 'transfer' else {
                    'reserve': 'ADMIT', 'reorder': 'ADMIT', 'start': 'START',
                    'ready': 'READY', 'claim': 'CLAIM', 'cancel': 'CANCEL'}[c['action']]
                op = dict(id=oid, sid=sid, action=c['action'], phase=phase, status='PENDING',
                          decision='UNDECIDED', message=PHASE_TEXT[phase], failure=None,
                          history=[], source=before_order['store_id'] if before_order else None,
                          target=after_order['store_id'], attempts=0, fault=fault, fault_consumed=False,
                          candidate=candidate, world=s['world_id'], order_id=after_order['id'],
                          before_generation=(before_order or {}).get('merchant_generation', 0),
                          generation=gen, request_id=c['request_id'], fingerprint=fp,
                          recovery=initial_schedule(time.time()))
                if c['action'] == 'transfer':
                    s['next_transfer_fault'] = candidate['next_transfer_fault'] = 'none'
                db.execute('INSERT INTO route_operations VALUES(?,?,?,?,?)',
                           (oid, sid, c['request_id'], fp, json.dumps(op, ensure_ascii=False)))
                s['active_operation'] = oid
                s['handoff'] = public_operation(op)
                s['version'] += 1
                self.store._save(db, s)
                db.execute('COMMIT')
            except BaseException:
                if db.in_transaction:
                    db.execute('ROLLBACK')
                raise
        return self.resume(sid, oid)

    def control(self, sid: str, c: dict):
        """Guest controls share the same durable recovery boundary as orders."""
        from .store import require, Conflict
        from .merchant_fleet import CAPACITY
        key = 'control:' + c['request_id']
        fp = digest(c)
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                s = self.store._load(db, sid)
                require('world_id' in s, 'LEGACY_JOURNEY', '새 체험에서 매장 변경을 확인해 주세요.')
                previous = db.execute('SELECT id,fingerprint FROM route_operations WHERE journey_id=? AND request_id=?', (sid,key)).fetchone()
                if previous:
                    require(previous[1] == fp, 'IDEMPOTENCY_CONFLICT', '이미 보낸 설정과 내용이 달라요.')
                    oid = previous[0]
                    db.execute('COMMIT')
                    return self.resume(sid, oid, duplicate=True)
                old = db.execute('SELECT fingerprint FROM route_controls WHERE journey_id=? AND request_id=?', (sid,c['request_id'])).fetchone()
                if old:
                    require(old[0] == fp, 'IDEMPOTENCY_CONFLICT', '이미 보낸 설정과 내용이 달라요.')
                    db.execute('COMMIT')
                    return self.store.view(s,True)
                require(s['version'] == c['expected_version'], 'STALE_VERSION', '주문 상태를 다시 확인해 주세요.')
                require(not s.get('active_operation'), 'TRANSFER_PENDING', '먼저 매장의 처리 결과를 확인해 주세요.')
                shop = c.get('store_id')
                require(shop in CAPACITY, 'UNKNOWN_STORE', '매장을 확인해 주세요.')
                require(not s['order'] or shop != s['order']['store_id'], 'SAME_STORE', '변경할 다른 매장을 선택해 주세요.')
                candidate = deepcopy(s)
                guests = candidate.setdefault('capacity_guests', {})
                if c['action'] == 'occupy':
                    require(sum(len(v) for v in guests.values()) < 4, 'GUEST_LIMIT', '체험 손님은 네 명까지 추가할 수 있어요.')
                    targets = ['GUEST-' + digest([sid,c['request_id']])[:24]]
                    guests.setdefault(shop, []).extend(targets)
                    phase = 'GUEST_ADMIT'
                else:
                    targets = list(guests.get(shop, []))
                    guests[shop] = []
                    phase = 'GUEST_CANCEL' if targets else 'FINALIZE'
                oid = uuid4().hex
                op = dict(id=oid, sid=sid, action='control_'+c['action'], phase=phase, status='PENDING',
                    decision='UNDECIDED', message=PHASE_TEXT[phase], failure=None, history=[],
                    source=None, target=shop, attempts=0, fault='none', fault_consumed=False,
                    candidate=candidate, world=s['world_id'], order_id=targets[0] if targets else 'EMPTY',
                    before_generation=0, generation=0, request_id=key, fingerprint=fp,
                    guests=targets, guest_index=0, recovery=initial_schedule(time.time()))
                db.execute('INSERT INTO route_operations VALUES(?,?,?,?,?)', (oid,sid,key,fp,json.dumps(op,ensure_ascii=False)))
                # Reserve the caller's idempotency key across both control paths.
                db.execute('INSERT INTO route_controls VALUES(?,?,?)', (sid,c['request_id'],fp))
                s['active_operation'] = oid
                s['handoff'] = public_operation(op)
                s['version'] += 1
                self._save_op(db, op)
                self.store._save(db,s)
                db.execute('COMMIT')
            except BaseException:
                if db.in_transaction: db.execute('ROLLBACK')
                raise
        result = self.resume(sid,oid)
        if result['handoff']['status'] == 'REJECTED':
            raise Conflict(result['handoff']['failure'], result['handoff']['message'])
        return result

    def resume(self, sid: str, oid: str, duplicate: bool = False,
               automatic: bool = False, now: float | None = None) -> dict:
        from .store import require
        # Each iteration commits one coordinator phase. Merchant commit is in
        # a different DB; an exception/process death cannot roll it back.
        for iteration in range(12):
            with self.store.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                try:
                    s, op = self.store._load(db, sid), self._op(db, oid)
                    require(op['sid'] == sid, 'UNKNOWN_OPERATION', '이 주문의 작업이 아니에요.')
                    if op['status'] != 'PENDING':
                        db.execute('COMMIT')
                        return self.store.view(s, duplicate)
                    require(s.get('active_operation') == oid, 'OPERATION_FENCE', '이미 다른 작업으로 바뀌었어요.')
                    tick = time.time() if now is None else now
                    if 'recovery' not in op:
                        op['recovery'] = initial_schedule(tick)
                        # Upgrade old pending records once; do not postpone them
                        # again on every scan or assume an unknown creation age.
                        op['recovery']['next_retry_at'] = tick
                    recovery = op['recovery']
                    if iteration == 0 and automatic and (recovery['state'] == 'REVIEW_REQUIRED'
                            or recovery['next_retry_at'] > tick):
                        db.execute('COMMIT')
                        return self.store.view(s, duplicate)
                    if iteration == 0 and not automatic and recovery['state'] == 'REVIEW_REQUIRED':
                        recovery.update(state='SCHEDULED', retry_count=0)
                    expire_undecided(op, tick)
                    op['attempts'] += 1
                    pause = self._step(op)
                    after_step(op, tick, pause)
                    if op['status'] == 'COMPLETED':
                        new = deepcopy(op['candidate'])
                        new['version'] = s['version'] + 1
                        s = new
                    else:
                        # Every visible phase/retry update is versioned so an old
                        # background GET cannot overwrite a newer command result.
                        s['version'] += 1
                    if op['status'] != 'PENDING':
                        s.pop('active_operation', None)
                    s['handoff'] = public_operation(op)
                    self._save_op(db, op)
                    self.store._save(db, s)
                    db.execute('COMMIT')
                except BaseException:
                    if db.in_transaction:
                        db.execute('ROLLBACK')
                    raise
            if pause or op['status'] != 'PENDING':
                return self.store.view(s, duplicate)
        return self.store.get(sid)

    def _merchant(self, op: dict, phase: str) -> dict:
        if phase in {'GUEST_ADMIT','GUEST_CANCEL'}:
            return self.store.fleet.execute(op['target'], world=op['world'],
                order_id=op['guests'][op['guest_index']], generation=0,
                operation_id=f"{op['id']}:{phase}:{op['guest_index']}",
                action='ADMIT' if phase == 'GUEST_ADMIT' else 'CANCEL')
        source_action = phase in {'FREEZE', 'RELEASE_SOURCE', 'UNFREEZE'}
        shop = op['source'] if source_action else op['target']
        gen = op['before_generation'] if source_action else op['generation']
        transport_options = {}
        if getattr(self.store.fleet, 'handles_response_loss', False):
            transport_options['lose_reply'] = op['fault'] == {
                'HOLD': 'after_target_hold', 'RELEASE_SOURCE': 'after_source_release',
                'ACTIVATE': 'after_target_activation'}.get(phase, '')
        return self.store.fleet.execute(shop, world=op['world'], order_id=op['order_id'],
            generation=gen, operation_id=f"{op['id']}:{phase}", action=phase,
            transfer_id=op['id'] if op['action'] == 'transfer' else '',
            reject=(phase == 'HOLD' and op['fault'] == 'target_reject'), **transport_options)

    @staticmethod
    def _next(op: dict, phase: str):
        op['phase'], op['message'] = phase, PHASE_TEXT.get(phase, '')

    def _step(self, op: dict) -> bool:
        phase = op['phase']
        if phase == 'FINALIZE':
            op['status'] = 'COMPLETED'
            op['message'] = '같은 주문으로 매장을 바꿨어요.' if op['action'] == 'transfer' else '처리를 마쳤어요.'
            op['history'].append(dict(phase=phase, label=PHASE_TEXT[phase], result='CONFIRMED'))
            return False
        if phase == 'DECIDE':
            op['decision'] = 'COMMIT'
            op['history'].append(dict(phase=phase, label=PHASE_TEXT[phase], result='COMMIT_RECORDED'))
            self._next(op, 'RELEASE_SOURCE')
            return False
        try:
            result = self._merchant(op, phase)
        except (sqlite3.OperationalError, OSError):
            op['message'] = '매장의 답을 확인하지 못했어요. 주문을 새로 만들지 말고 다시 확인해 주세요.'
            return True
        lost = {'HOLD': 'after_target_hold', 'RELEASE_SOURCE': 'after_source_release',
                'ACTIVATE': 'after_target_activation'}.get(phase)
        if (not getattr(self.store.fleet, 'handles_response_loss', False)
                and result['ok'] and lost and op['fault'] == lost and not op['fault_consumed']):
            # Merchant really committed. Deliberately discard the reply once,
            # leaving this phase unacknowledged, exactly as after a lost response.
            op['fault_consumed'] = True
            op['message'] = '응답이 끊겨 결과를 아직 확인하지 못했어요. 같은 주문으로 다시 확인해 주세요.'
            op['history'].append(dict(phase=phase, label=PHASE_TEXT[phase], result='REPLY_NOT_CONFIRMED'))
            return True
        if not result['ok'] and op['action'].startswith('control_'):
            op['status'] = 'REJECTED'
            op['failure'] = result['code']
            op['message'] = '매장의 상태가 달라 체험 설정을 완료하지 못했어요. 고객 주문과 혜택은 바꾸지 않았어요.'
            return False
        if result['ok'] and phase in {'GUEST_ADMIT','GUEST_CANCEL'}:
            op['history'].append(dict(phase=phase, label=PHASE_TEXT[phase], result='CONFIRMED'))
            op['guest_index'] += 1
            if op['guest_index'] >= len(op['guests']): self._next(op,'FINALIZE')
            return False
        if not result['ok']:
            op['failure'] = result['code']
            if op['action'] == 'transfer' and phase == 'HOLD' and op['decision'] != 'COMMIT':
                op['decision'] = 'ABORT'
                self._next(op, 'ABORT_TARGET')
                return False
            if op['action'] != 'transfer' or phase == 'FREEZE':
                op['status'] = 'REJECTED'
                op['message'] = (ERROR_TEXT.get(result['code'], '현재 매장의 상태가 달라 변경하지 않았어요.')
                                 if op['action'] == 'transfer' else
                                 '매장에서 처리하지 못했어요. 이 요청으로 주문이나 혜택을 새로 확정하지 않았어요.')
                return False
            # Once COMMIT is durable, never roll back into a second active owner.
            op['message'] = '매장 기록을 더 확인해야 해요. 중복 주문을 만들지 않고 현재 작업을 유지해요.'
            return True
        op['history'].append(dict(phase=phase, label=PHASE_TEXT[phase], result='CONFIRMED'))
        if phase == 'UNFREEZE':
            op['status'] = 'REJECTED'
            op['message'] = ERROR_TEXT.get(op['failure'], '변경하지 못해 원래 주문과 혜택을 유지했어요.')
        else:
            self._next(op, {'FREEZE': 'ABORT_TARGET' if op['decision'] == 'ABORT' else 'HOLD', 'HOLD': 'DECIDE', 'RELEASE_SOURCE': 'ACTIVATE',
                            'ACTIVATE': 'FINALIZE', 'ABORT_TARGET': 'UNFREEZE'}.get(phase, 'FINALIZE'))
        return False
