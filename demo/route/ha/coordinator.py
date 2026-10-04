"""V2 domain protocol over native journey transactions and fenced work leases."""
from copy import deepcopy
from uuid import uuid4
from ..store import require, Conflict
from ..planner import digest
from ..payment_operations import PaymentOperations, ACTIONS
from ..payment_steps import TEXT, FAULTS, public, expire, transition
from ..payments.domain import PaymentError
from ..durable_operations import DurableOperations, FAULTS as MERCHANT_FAULTS, PHASE_TEXT
from ..merchant_fleet import CAPACITY
from ..recovery_worker import initial_schedule, after_step


class PostgresCoordinator(PaymentOperations):
    # _plan, _external, _observe and _finalize are the same v2 domain functions.
    _merchant = DurableOperations._merchant

    def __init__(self, store, worker_id, lease_seconds=30):
        super().__init__(store)
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 120:
            raise ValueError('bounded operation lease required')
        self.worker_id, self.lease_seconds = worker_id, lease_seconds

    def command(self, sid, c):
        fp, oid, duplicate = digest(c), None, False
        with self.store.repository.unit(sid) as u:
            s = u.state
            old = u.request('command', c['request_id'])
            if old:
                require(old['fingerprint'] == fp, 'IDEMPOTENCY_CONFLICT', '같은 요청 번호의 내용이 달라요.')
                oid, duplicate = old['operation_id'], True
            else:
                require(s['version'] == c['expected_version'], 'STALE_VERSION', '주문 상태를 다시 확인해 주세요.')
                if c['action'] == 'recover':
                    oid = s.get('active_operation')
                    if oid:
                        row = u.operation(oid)
                        op = row['payload']
                        if op['recovery']['state'] == 'REVIEW_REQUIRED':
                            require(not row['leased'], 'TRANSFER_PENDING', '현재 작업의 처리를 확인하고 있어요.')
                            require(op.get('manual_reviews', 0) < 3, 'REVIEW_LIMIT', '이 작업은 추가 운영 확인이 필요해요.')
                            op['manual_reviews'] = op.get('manual_reviews', 0) + 1
                            op['recovery'].update(state='SCHEDULED', retry_count=0, next_retry_at=u.now())
                            op['phase_version'] += 1
                            u.replace_unleased(op)
                    u.record('command', c['request_id'], fp, oid)
                else:
                    require(not s.get('active_operation'), 'TRANSFER_PENDING', '매장·결제 확인이 끝나지 않았어요. 다시 결제하지 마세요.')
                    candidate = deepcopy(s)
                    self.store._apply(candidate, c)
                    if c['action'] not in ACTIONS:
                        candidate['version'] += 1
                        u.save(candidate)
                        u.record('command', c['request_id'], fp)
                        s = candidate
                    elif c['action'] in {'claim', 'cancel'} and candidate['order'] == s['order']:
                        u.record('command', c['request_id'], fp)
                        duplicate = True
                    else:
                        op = self._plan(s, c, candidate, fp)
                        op['recovery'] = initial_schedule(u.now())
                        oid = op['id']
                        for auth in (op['new_auth'], op['old_auth']):
                            if auth:
                                u.register(auth)
                        u.enqueue(op)
                        u.record('command', c['request_id'], fp, oid)
                        s.update(active_operation=oid, pending_order_id=op['order_id'], payment_order_id=op['order_id'])
                        s['handoff'] = public(op)
                        s['version'] += 1
                        u.save(s)
        return self.resume(sid, oid, duplicate=duplicate) if oid else self.store.view(s, duplicate)

    def control(self, sid, c):
        action = c.get('action')
        fields = {'action', 'expected_version', 'request_id'} | ({'fault'} if action == 'fault' else {'card_token'})
        require(action in {'fault', 'card'} and set(c) == fields, 'INVALID_PAYMENT_CONTROL', '가상 결제 설정을 확인해 주세요.')
        if action == 'fault':
            require(c['fault'] in FAULTS, 'INVALID_PAYMENT_FAULT', '지원하지 않는 결제 상황이에요.')
        else:
            require(c['card_token'] in {'demo-approved', 'demo-declined'}, 'INVALID_TEST_CARD', '가상 카드만 선택할 수 있어요.')
        fp = digest(c)
        with self.store.repository.unit(sid) as u:
            s = u.state
            old = u.request('payment', c['request_id'])
            if old:
                require(old['fingerprint'] == fp, 'IDEMPOTENCY_CONFLICT', '이미 보낸 설정과 내용이 달라요.')
            else:
                require(s['version'] == c['expected_version'], 'STALE_VERSION', '주문 상태를 다시 확인해 주세요.')
                require(not s.get('active_operation'), 'TRANSFER_PENDING', '진행 중인 작업을 먼저 확인해 주세요.')
                if action == 'card':
                    require(s['order'] is None, 'PAYMENT_CARD_LOCKED', '주문 후에는 가상 카드를 바꾸지 않아요.')
                s['next_payment_fault' if action == 'fault' else 'payment_card'] = c['fault' if action == 'fault' else 'card_token']
                s['version'] += 1
                u.save(s)
                u.record('payment', c['request_id'], fp)
        return self.store.view(s, bool(old))

    def transfer_control(self, sid, c):
        fp, oid = digest(c), None
        with self.store.repository.unit(sid) as u:
            s = u.state
            old = u.request('transfer', c['request_id'])
            if old:
                require(old['fingerprint'] == fp, 'IDEMPOTENCY_CONFLICT', '이미 보낸 설정과 내용이 달라요.')
                oid = old['operation_id']
            else:
                require(s['version'] == c['expected_version'], 'STALE_VERSION', '주문 상태를 다시 확인해 주세요.')
                require(not s.get('active_operation'), 'TRANSFER_PENDING', '먼저 매장의 처리 결과를 확인해 주세요.')
                if c['action'] == 'fault':
                    require(c.get('fault') in MERCHANT_FAULTS, 'INVALID_COMMAND', '지원하지 않는 체험 설정이에요.')
                    s['next_transfer_fault'] = c['fault']
                else:
                    require(c['action'] in {'occupy', 'clear'}, 'INVALID_COMMAND', '지원하지 않는 체험 설정이에요.')
                    shop = c.get('store_id')
                    require(shop in CAPACITY, 'UNKNOWN_STORE', '매장을 확인해 주세요.')
                    require(not s['order'] or shop != s['order']['store_id'], 'SAME_STORE', '변경할 다른 매장을 선택해 주세요.')
                    candidate = deepcopy(s)
                    guests = candidate.setdefault('capacity_guests', {})
                    if c['action'] == 'occupy':
                        require(sum(len(v) for v in guests.values()) < 4, 'GUEST_LIMIT', '체험 손님은 네 명까지 추가할 수 있어요.')
                        targets = ['GUEST-' + digest([sid, c['request_id']])[:24]]
                        guests.setdefault(shop, []).extend(targets)
                        phase = 'GUEST_ADMIT'
                    else:
                        targets = list(guests.get(shop, []))
                        guests[shop] = []
                        phase = 'GUEST_CANCEL' if targets else 'FINALIZE'
                    oid = uuid4().hex
                    op = dict(id=oid, sid=sid, protocol_version=2, action='control_'+c['action'], phase=phase,
                              phase_version=0, status='PENDING', decision='UNDECIDED', message=TEXT[phase],
                              failure=None, history=[], source=None, target=shop, attempts=0, fault='none',
                              fault_consumed=False, candidate=candidate, world=s['world_id'],
                              order_id=targets[0] if targets else 'EMPTY', before_generation=0, generation=0,
                              request_id='control:'+c['request_id'], fingerprint=fp, guests=targets,
                              guest_index=0, recovery=initial_schedule(u.now()), results={})
                    u.enqueue(op)
                    s['active_operation'] = oid
                    s['handoff'] = public(op)
                s['version'] += 1
                u.save(s)
                u.record('transfer', c['request_id'], fp, oid)
        result = self.resume(sid, oid, duplicate=bool(old)) if oid else self.store.view(s, bool(old))
        if oid and result['handoff']['status'] == 'REJECTED':
            raise Conflict(result['handoff']['failure'], result['handoff']['message'])
        return result

    def _prepare_claim(self, sid, oid, owner, automatic, now=None):
        with self.store.repository.unit(sid) as u:
            row = u.operation(oid)
            op, s = row['payload'], u.state
            if row['status'] != 'PENDING' or row['leased']:
                return None
            require(s.get('active_operation') == oid, 'OPERATION_FENCE', '이미 다른 작업으로 바뀌었어요.')
            if op['recovery']['state'] == 'REVIEW_REQUIRED' or (automatic and row['not_due']):
                return None
            tick = u.now() if now is None else now
            if expire(op, tick):
                op['phase_version'] += 1
                u.replace_unleased(op)
                s['version'] += 1
                s['handoff'] = public(op)
                u.save(s)
            return u.claim(oid, owner, self.lease_seconds, automatic)

    def resume(self, sid, oid, duplicate=False, automatic=False, now=None):
        if oid is None:
            return self.store.get(sid)
        owner = self.worker_id + ':' + uuid4().hex
        for iteration in range(16):
            claim = self._prepare_claim(sid, oid, owner, automatic and iteration == 0, now)
            if claim is None:
                break
            op = claim['payload']
            phase = op['phase']
            try:
                result = self._external(op)
            except PaymentError as exc:
                result = dict(ok=False, code=exc.code)
            except OSError:
                result = None
            with self.store.repository.unit(sid) as u:
                current = u.operation(oid)
                if (current['lease_version'] != claim['lease_version'] or current['phase_version'] != claim['phase_version']
                        or current['lease_owner'] != owner or not current['leased']
                        or current['status'] != 'PENDING' or u.state.get('active_operation') != oid):
                    continue
                s = u.state
                op['attempts'] += 1
                if result is None:
                    op['message'] = '매장·결제 응답을 확인하지 못했어요. 기존 작업을 다시 확인하고 있어요.'
                    op['history'].append(dict(phase=phase, label=TEXT[phase], result='RESPONSE_NOT_CONFIRMED'))
                else:
                    self._observe(s, op, phase, result)
                    op['results'][phase] = deepcopy(result)
                    op['history'].append(dict(phase=phase, label=TEXT[phase], result='CONFIRMED' if result['ok'] else result.get('code', 'REJECTED')))
                    if op['action'].startswith('control_') and phase in {'GUEST_ADMIT', 'GUEST_CANCEL'}:
                        if not result['ok']:
                            op.update(status='REJECTED', failure=result['code'], message='체험 손님의 처리 상태가 달라 완료하지 않았어요.')
                        else:
                            op['guest_index'] += 1
                            if op['guest_index'] >= len(op['guests']):
                                op.update(phase='FINALIZE', message=TEXT['FINALIZE'])
                    else:
                        transition(op, result)
                after_step(op, u.now() if now is None else now, result is None)
                if op['status'] == 'COMPLETED':
                    if op['action'].startswith('control_'):
                        s = deepcopy(op['candidate'])
                    else:
                        s = self._finalize(s, op)
                elif op['status'] == 'REJECTED' and s['order'] is None and not op['action'].startswith('control_'):
                    s['payment']['state'] = 'DECLINED' if op['failure'] in {'DECLINED', 'LIMIT_EXCEEDED'} else 'VOIDED'
                s['version'] = u.state['version'] + 1
                op['phase_version'] += 1
                if op['status'] != 'PENDING':
                    s.pop('active_operation', None)
                    s.pop('pending_order_id', None)
                s['handoff'] = public(op)
                applied = u.apply(claim, op, s)
            if not applied:
                continue
            if result is None or op['status'] != 'PENDING' or op['recovery']['state'] == 'REVIEW_REQUIRED':
                break
        return self.store.view(self.store.read_state(sid), duplicate)
