"""Run three policies through real command handlers under six explicit schedules.

This is a controlled executable comparison, not a market benchmark. Cancel /
reorder checks conditions first and retries uncertain commands idempotently.
No artificial click delay or monetary penalty is added to the baseline.
"""
from __future__ import annotations
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from time import perf_counter
from uuid import uuid4
from .planner import digest, plans
from . import benefits

POLICIES = ['stay', 'cancel_reorder', 'reserve_first_reorder', 'guarded_transfer']
SCENARIOS = {
    'normal': '새 매장이 정상적으로 받을 때',
    'reject_after_quote': '견적 확인 뒤 새 매장이 주문을 거절할 때',
    'last_place_taken': '견적 확인 뒤 다른 손님이 마지막 자리를 잡을 때',
    'reply_lost': '새 매장의 수락 응답이 한 번 끊길 때',
    'already_preparing': '원래 매장이 이미 만들기 시작했을 때',
    'coupon_expired': '보류된 전 매장 쿠폰의 사용 기간이 지났을 때',
}


def _one(directory: Path, intent: dict, scenario: str, policy: str, shared_store=None) -> tuple[dict, dict]:
    from .store import JourneyStore, Conflict
    st = shared_store or JourneyStore(directory / 'orders.sqlite')
    st.fleet.after_commit = None  # A failure hook never crosses isolated worlds.
    effective = deepcopy(intent)
    if scenario == 'coupon_expired':
        effective.update(coupon_id='morning10', deadline_minutes=90)
    s = st.create(effective)
    trace, requests, recoveries = [], 0, 0
    def act(action, **extra):
        nonlocal s, requests, recoveries
        request = dict(action=action, expected_version=s['version'], request_id=uuid4().hex, **extra)
        requests += 1
        s = st.command(s['id'], request)
        trace.append(dict(action=action, status=s.get('handoff', {}).get('status'),
                          order_id=(s['order'] or {}).get('id'), order_state=(s['order'] or {}).get('state')))
        if s.get('handoff_pending'):
            recoveries += 1
            requests += 1
            s = st.command(s['id'], request)
            trace.append(dict(action='same_request_retry', status=s['handoff']['status'],
                              order_id=(s['order'] or {}).get('id'), order_state=(s['order'] or {}).get('state')))
        return s
    def q(shop):
        return next(p for p in s['all_plans'] if p['store_id'] == shop)
    if not s['recommendations']:
        return dict(has_order=False, same_order=False, original_preserved=False, cash_due=0,
                    predicted_arrival=None, meets_modeled_deadline=False, customer_commands=0,
                    same_request_retries=0, authorization_count=0, capture_count=0,
                    held_points=0, points_spent=0, reason='NO_INITIAL_ROUTE'), dict(intent=effective, commands=trace)
    act('reserve', quote_id=s['recommendations'][0]['quote_id'])
    original = deepcopy(s['order'])
    if scenario == 'already_preparing':
        act('advance', minutes=max(0, s['current_plan']['start_at']-s['clock']))
        act('start')
    if scenario == 'coupon_expired':
        act('advance', minutes=30)
    act('disrupt', store_id=s['order']['store_id'], minutes=12)
    # Costs below count only the choice after the common initial ordering work.
    requests = 0
    targets = [p for p in s['all_plans'] if p['feasible'] and p['store_id'] != s['order']['store_id']]
    candidate = min(targets, key=lambda p: (p['arrival_at'], p['price'], p['store_id'])) if targets else None
    reason = 'STAY'
    old_wallet = deepcopy(s['wallet'])
    preserved_order = deepcopy(s['order'])
    observed = dict(clock=s['clock'], source=s['order']['store_id'],
                    target=candidate['store_id'] if candidate else None,
                    original_cash=s['order']['price'], target_cash=candidate['price'] if candidate else None,
                    state=s['order']['state'])
    if policy != 'stay' and s['order']['state'] != 'RESERVED':
        reason = 'ALREADY_PREPARING'
    elif policy != 'stay' and not candidate:
        reason = 'NO_ELIGIBLE_ALTERNATIVE'
    elif policy != 'stay':
        target = candidate['store_id']
        if policy in {'cancel_reorder','reserve_first_reorder'}:
            # A competent baseline checks expected post-cancellation benefits
            # before cancelling; it does not knowingly violate a cash limit.
            with st.connection() as db:
                preview = st._load(db, s['id'])
            preview['order'] = None
            benefits.release(preview)
            estimated = next(p for p in plans(preview) if p['store_id'] == target)
            if not estimated['feasible']:
                reason = 'REORDER_PREFLIGHT_REJECTED'
                candidate = None
        if candidate:
            observed['target_available_at_preflight'] = st.fleet.snapshot(s['world_id'])[target]['available']
            # The adverse event happens AFTER the identical preflight. Merely
            # checking availability twice cannot lock the time between calls.
            if scenario == 'reject_after_quote':
                st.fleet.set_accepting(target, s['world_id'], False)
            elif scenario == 'last_place_taken':
                available = st.fleet.snapshot(s['world_id'])[target]['available']
                for n in range(available):
                    result = st.fleet.execute(target,world=s['world_id'],order_id=f'OTHER-{n}',generation=0,
                                             operation_id=f'other-admit-{n}',action='ADMIT')
                    assert result['ok']
            elif scenario == 'reply_lost':
                dropped = False
                def lose_reply(shop, action, key, result):
                    nonlocal dropped
                    if not dropped and shop == target and action in {'HOLD', 'ADMIT'} and result['ok']:
                        dropped = True
                        raise OSError('controlled reply loss after committed merchant acceptance')
                st.fleet.after_commit = lose_reply
            try:
                if policy == 'guarded_transfer':
                    act('transfer', quote_id=q(target)['quote_id'])
                elif policy == 'reserve_first_reorder':
                    act('reserve_first_reorder', quote_id=q(target)['quote_id'], accepted_cash_due=estimated['price'])
                else:
                    act('cancel')
                    act('reorder', quote_id=q(target)['quote_id'])
                reason = s['handoff']['failure'] or s['handoff']['status']
            except Conflict as exc:
                reason = exc.code
                s = st.get(s['id'])
    order = s['order']
    has_order = bool(order and order['state'] not in {'CANCELLED'}) and not s.get('handoff_pending')
    cp = s['current_plan'] if has_order else None
    metrics = dict(has_order=has_order, same_order=bool(has_order and order['id'] == original['id']),
        original_preserved=bool(has_order and order == preserved_order and s['wallet'] == old_wallet),
        cash_due=order['price'] if has_order else 0,
        predicted_arrival=cp['arrival_at'] if cp else None,
        meets_modeled_deadline=bool(cp and cp['arrival_at'] + 1 <= effective['deadline_minutes']),
        customer_commands=requests, same_request_retries=recoveries,
        authorization_count=s['receipt']['authorization_count'], capture_count=s['receipt']['capture_count'],
        held_points=s['wallet']['held_points'], points_spent=s['wallet']['spent'], reason=reason)
    raw = dict(world_id=s['world_id'],intent=effective, observations=observed, commands=trace,
               original_order_id=original['id'], final_order_id=(order or {}).get('id'),
               final_merchant_capacity=s['merchant_capacity'], final_wallet=s['wallet'],
               operation=s.get('handoff'))
    return metrics, raw


def run_transfer_comparison(intent: dict) -> dict:
    from .store import JourneyStore
    cases, executions = [], []
    began=perf_counter()
    with tempfile.TemporaryDirectory(prefix='pickup-pact-comparison-') as tmp:
        root = Path(tmp)
        shared_store=JourneyStore(root/'orders.sqlite')
        initialized=perf_counter()
        for scenario, label in SCENARIOS.items():
            row = dict(scenario=scenario, label=label)
            for policy in POLICIES:
                metrics, raw = _one(root / scenario / policy, intent, scenario, policy, shared_store=shared_store)
                row[policy] = metrics
                executions.append(dict(scenario=scenario, policy=policy, **raw))
            cases.append(row)
    semantic = dict(intent=intent, policies=POLICIES, cases=cases)
    return dict(**semantic, semantic_sha256=digest(semantic), executions=executions,
        mode='controlled_command_execution',
        timing=dict(setup_seconds=round(initialized-began,6),total_seconds=round(perf_counter()-began,6),isolated_worlds=len(executions),storage_durability='SQLite WAL, synchronous FULL (merchant)'),
        baseline_design='새 자리 확보 후 재주문도 같은 저장된 이관 엔진을 사용해요. 독립적인 경쟁 서비스 구현이 아니며, 안전 절차가 같으면 주문 보존 결과도 같을 수 있어요.',
        disclosure='동일한 가상 조건에서 실제 주문 코드를 실행했어요. 취소·재주문도 조건 확인과 중복 방지 재시도를 사용해요. 시간 차이와 수수료를 임의로 더하지 않았어요. 6가지 상황의 결과이며 실제 이용 빈도나 다른 서비스의 성능을 뜻하지 않아요.')
