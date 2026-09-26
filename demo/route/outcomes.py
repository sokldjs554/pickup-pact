"""Paired policy experiments, not measured delivery performance.

Both policies share each generated environment and per-store future errors.
Only the observed snapshot goes into choose_transfer(). The future error is
revealed AFTER selection to score modeled arrival, including regressions.
"""
from __future__ import annotations
from copy import deepcopy
from functools import lru_cache
from random import Random
from statistics import mean

from .benefits import hold, initial_wallet
from .planner import STORES, digest, plans

SCENARIOS = ('normal', 'source_busy', 'all_busy', 'after_start', 'coupon_budget', 'uncertain_delay')
SCENARIO_LABELS = dict(zip(SCENARIOS, ('혼잡 없음', '주문 매장만 혼잡', '대체 매장도 혼잡',
                                    '이미 제조 시작', '쿠폰 상실·예산 제한', '선택 후 예상 밖 지연')))


def current_plan(state: dict) -> dict | None:
    order = state.get('order')
    if not order:
        return None
    p = next(p for p in plans(state) if p['store_id'] == order['store_id'])
    if order['state'] in {'PREPARING', 'READY', 'PICKED_UP'}:
        p['ready_at'] = order['ready_at']
        p['pickup_at'] = (order.get('picked_up_at', order['ready_at']) if order['state'] == 'PICKED_UP'
                          else max(state['clock'], order['departure_at'] + state['arrival_delay'] + p['walk_to'], order['ready_at']))
        p['arrival_at'] = p['pickup_at'] + p['walk_after']
        p['margin'] = state['intent']['deadline_minutes'] - p['arrival_at']
        p['feasible'] = p['margin'] >= 1 and not [r for r in p['reasons'] if r not in {'DEADLINE', 'OFFLINE', 'MENU'}]
    return p


def compare_order(state: dict) -> dict:
    source = current_plan(state)
    order = state.get('order')
    mutable = bool(order and order['state'] == 'RESERVED')
    alternatives = []
    if source and mutable:
        for p in plans(state):
            if p['store_id'] == order['store_id'] or not p['feasible']:
                continue
            alternatives.append(dict(store_id=p['store_id'], name=p['name'], quote_id=p['quote_id'],
                                     arrival_at=p['arrival_at'], cash_due=p['price'],
                                     minutes_saved=source['arrival_at'] - p['arrival_at'],
                                     cash_delta=p['price'] - order['price'],
                                     coupon_loss=max(0, source['pricing']['coupon_discount'] - p['pricing']['coupon_discount']),
                                     pricing=p['pricing']))
    return dict(basis='synthetic_snapshot', version=state['version'], order_id=order['id'] if order else None,
                snapshot_hash=digest(state), transferable=mutable,
                stay=dict(store_id=source['store_id'], name=source['name'], arrival_at=source['arrival_at'],
                          cash_due=order['price'], margin=source['margin']) if source else None,
                alternatives=alternatives,
                limitation='같은 현재 증거로 계산한 예상 비교입니다. 실제 도착 성과나 패스오더 알고리즘 비교가 아닙니다.')


def choose_transfer(observed: dict) -> tuple[str | None, str]:
    """Risk-triggered policy. Never accepts an unobserved future-error input."""
    order = observed.get('order')
    if not order:
        return None, 'NO_INITIAL_ROUTE'
    if order['state'] != 'RESERVED':
        return None, 'ALREADY_PREPARING'
    source = current_plan(observed)
    if source['feasible']:
        return None, 'STAY_IS_FEASIBLE'
    options = [p for p in plans(observed) if p['feasible'] and p['store_id'] != order['store_id']]
    if not options:
        return None, 'NO_ELIGIBLE_ALTERNATIVE'
    target = min(options, key=lambda p: (p['arrival_at'], p['price'], p['store_id']))
    if target['arrival_at'] >= source['arrival_at']:
        return None, 'NO_TIME_GAIN'
    return target['store_id'], 'TRANSFER'


def make_case(seed: int, index: int) -> dict:
    rng = Random(seed * 100003 + index)
    scenario = SCENARIOS[index % len(SCENARIOS)]
    intent = dict(destination='office', deadline_minutes=rng.randint(10, 24), drink='latte',
                  milk=rng.choice(['regular', 'regular', 'oat']), decaf=rng.random() < .15,
                  budget=rng.choice([2500, 3500, 4500, 5500, 6500]), max_detour=rng.choice([3, 5, 10]),
                  priority='arrival', coupon_id=rng.choice([None, 'welcome500', 'wave1000']),
                  points=rng.choice([0, 500, 1000, 2000]))
    if scenario == 'coupon_budget':
        intent.update(coupon_id='wave1000', budget=3000, points=500, milk='regular', decaf=False)
    if scenario == 'uncertain_delay':
        intent.update(budget=6500, deadline_minutes=16, milk='regular', decaf=False)
    s = dict(id=f'case-{seed}-{index}', version=1, clock=0, arrival_delay=0,
             intent=intent, stores=deepcopy(STORES), order=None, events=[], wallet=initial_wallet())
    for store in s['stores']:
        # Keep the published v1 population independent of later partner policy.
        # New partner eligibility is assessed in the four-policy command trial.
        store.pop('transfer_policy',None)
        store['queue_until'] = rng.randint(0, 5)
    initial = next((p for p in plans(s) if p['feasible']), None)
    if initial:
        s['order'] = dict(id=f'order-{seed}-{index}', store_id=initial['store_id'], store_name=initial['name'],
                          state='RESERVED', price=initial['price'], pricing=deepcopy(initial['pricing']),
                          plan=deepcopy(initial), ready_at=initial['ready_at'], departure_at=0)
        hold(s, initial['pricing'])
        store = next(v for v in s['stores'] if v['id'] == initial['store_id'])
        if scenario in {'source_busy', 'coupon_budget', 'uncertain_delay'}:
            store['queue_until'] += rng.randint(5, 18) if scenario != 'uncertain_delay' else 9
        elif scenario == 'all_busy':
            for v in s['stores']:
                v['queue_until'] = 40 + rng.randint(0, 10)
        elif scenario == 'after_start':
            s['clock'] = initial['start_at']
            s['order'].update(state='PREPARING', ready_at=initial['ready_at'] + rng.randint(5, 18))
    s['version'] = 2
    errors = {v['id']: rng.randint(0, 12 if scenario == 'uncertain_delay' else 3) for v in s['stores']}
    return dict(case_id=f'{seed}:{index}', seed=seed, index=index, scenario=scenario,
                observed=s, future_delays=errors)


def evaluate_case(case: dict) -> dict:
    s = case['observed']
    source = current_plan(s)
    selected, reason = choose_transfer(s)
    target = next((p for p in plans(s) if p['store_id'] == selected), source)
    environment_hash = digest(dict(observed=s, future_delays=case['future_delays']))

    def outcome(p):
        arrival = p['arrival_at'] + case['future_delays'][p['store_id']] if p else None
        return dict(environment_hash=environment_hash, initial_store=source['store_id'] if source else None,
                    initially_serviceable=source is not None, selected_store=p['store_id'] if p else None,
                    estimated_arrival=p['arrival_at'] if p else None, arrival_at=arrival,
                    on_time=arrival is not None and arrival <= s['intent']['deadline_minutes'],
                    cash_due=p['price'] if p else 0,
                    coupon_discount=p['pricing']['coupon_discount'] if p else 0,
                    points_used=p['pricing']['points_used'] if p else 0)

    return dict(case_id=case['case_id'], seed=case['seed'], scenario=case['scenario'],
                intent=s['intent'], state_at_decision=s['order']['state'] if s['order'] else 'NO_ORDER',
                observed_queues={v['id']: v['queue_until'] for v in s['stores']},
                future_delays=case['future_delays'], transferred=selected is not None,
                decision_reason=reason, stay=outcome(source), transfer=outcome(target))


def summarize(rows: list[dict]) -> dict:
    placed = [r for r in rows if r['stay']['initially_serviceable']]
    deltas = [r['transfer']['cash_due'] - r['stay']['cash_due'] for r in placed]
    return dict(total=len(rows), initially_serviceable=len(placed),
                no_initial_route=len(rows) - len(placed),
                stay_on_time=sum(r['stay']['on_time'] for r in rows),
                transfer_on_time=sum(r['transfer']['on_time'] for r in rows),
                wins=sum(r['transfer']['on_time'] and not r['stay']['on_time'] for r in rows),
                losses=sum(r['stay']['on_time'] and not r['transfer']['on_time'] for r in rows),
                ties=sum(r['stay']['on_time'] == r['transfer']['on_time'] for r in rows),
                transfers=sum(r['transferred'] for r in rows),
                mean_cash_delta=round(mean(deltas), 2) if deltas else 0,
                max_cash_increase=max([0] + deltas),
                both_late_or_unserviceable=sum(not r['stay']['on_time'] and not r['transfer']['on_time'] for r in rows))


@lru_cache(maxsize=8)
def run_experiment(seed: int = 7, cases: int = 120) -> dict:
    if type(seed) is not int or not 0 <= seed <= 1000000 or type(cases) is not int or not 6 <= cases <= 200:
        raise ValueError('seed/case bounds violated')
    rows = [evaluate_case(make_case(seed, i)) for i in range(cases)]
    summary = summarize(rows)
    summary['by_scenario'] = {name: dict(label=SCENARIO_LABELS[name], **summarize([r for r in rows if r['scenario'] == name]))
                              for name in SCENARIOS}
    return dict(basis='paired_synthetic_policy_experiment_v1', seed=seed, summary=summary, cases=rows,
                semantic_sha256=digest(rows),
                methodology=dict(baseline='기존 주문 매장 유지', treatment='위험 시 조건을 만족하는 매장으로 제조 전 이동',
                    denominator='생성한 모든 사례 포함. 처음부터 주문 불가·양쪽 실패도 제외하지 않음.',
                    noise='선택 시점에 알려지지 않은 매장별 추가 지연 0~3분, 불확실성 시나리오 0~12분.',
                    distribution='6개 의도적 시나리오를 순환. 실제 시장 빈도를 대표하지 않음.',
                    payment='모의 최종 결제 예정액. 실제 결제 또는 실제 도착 데이터가 아님.',
                    capacity='매장 대기시간으로 합성 혼잡 표현. 여러 고객의 공유 용량 운영 실험은 아님.',
                    movement='고정 출발지 기준 경로. 실제 이동 중 GPS 재경로 탐색 아님.'))
