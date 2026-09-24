"""Synthetic, journey-scoped benefit accounting. Never real money or loyalty.

Coupons discount the gross amount first; points offset the remainder. A hold
is not a spend. The enclosing JourneyStore transaction owns all mutations.
"""
from __future__ import annotations
from copy import deepcopy

INITIAL_POINTS = 2000
COUPONS = [
    dict(id='welcome500', name='첫 한 잔 500원', kind='fixed', value=500,
         minimum=3000, maximum=500, stores=[], expires_at=180),
    dict(id='wave1000', name='웨이브 전용 1,000원', kind='fixed', value=1000,
         minimum=4000, maximum=1000, stores=['wave'], expires_at=60),
    dict(id='morning10', name='모닝 10% 할인', kind='percent', value=10,
         minimum=3000, maximum=800, stores=[], expires_at=30),
]
REASONS = {
    'NONE': '쿠폰을 선택하지 않았어요.', 'APPLIED': '쿠폰이 적용됐어요.',
    'STORE_MISMATCH': '이 매장에서는 선택한 쿠폰을 사용할 수 없어요.',
    'MINIMUM': '쿠폰의 최소 주문 금액에 미달해요.',
    'EXPIRED': '체험 시각 기준으로 만료된 쿠폰이에요.',
    'USED': '이미 사용한 쿠폰이에요.',
}


def initial_wallet() -> dict:
    return dict(scope='journey_demo', balance=INITIAL_POINTS, held_points=0,
                held_coupon=None, used_coupons=[], spent=0, earned=0)


def wallet_for(state: dict) -> dict:
    # Compatibility for pre-benefit persisted journeys; no retroactive award
    # or deduction is made to their old orders.
    return state.get('wallet', initial_wallet())


def price_quote(state: dict, store_id: str, gross: int) -> dict:
    wallet = wallet_for(state)
    selection = state['intent'].get('coupon_id')
    coupon = next((c for c in COUPONS if c['id'] == selection), None)
    discount = 0
    reason = 'NONE'
    if coupon:
        if selection in wallet['used_coupons']:
            reason = 'USED'
        elif state['clock'] >= coupon['expires_at'] and wallet['held_coupon'] != selection:
            reason = 'EXPIRED'
        elif coupon['stores'] and store_id not in coupon['stores']:
            reason = 'STORE_MISMATCH'
        elif gross < coupon['minimum']:
            reason = 'MINIMUM'
        else:
            reason = 'APPLIED'
            discount = min(gross, coupon['maximum'], coupon['value'] if coupon['kind'] == 'fixed'
                           else gross * coupon['value'] // 100)
    requested = state['intent'].get('points', 0)
    # This journey's existing hold may be replaced atomically by an alternate
    # quote, so it remains available to the same order (not another order).
    points = min(requested, wallet['balance'], max(0, gross - discount))
    cash = gross - discount - points
    return dict(gross=gross, selected_coupon_id=selection,
                coupon_id=selection if discount else None,
                coupon_name=coupon['name'] if coupon else None,
                coupon_discount=discount, coupon_reason=reason,
                coupon_message=REASONS[reason], points_requested=requested,
                points_used=points, points_clipped=points != requested,
                cash_due=cash, points_to_earn=cash // 100,
                policy='coupon_then_points_v1', unit='KRW')


def wallet_view(state: dict) -> dict:
    wallet = deepcopy(wallet_for(state))
    wallet['available_points'] = wallet['balance'] - wallet['held_points']
    wallet['coupons'] = []
    for template in COUPONS:
        c = deepcopy(template)
        c['status'] = ('USED' if c['id'] in wallet['used_coupons'] else
                       'HELD' if c['id'] == wallet['held_coupon'] else
                       'EXPIRED' if state['clock'] >= c['expires_at'] else 'AVAILABLE')
        wallet['coupons'].append(c)
    return wallet


def legacy_pricing(order: dict) -> dict:
    gross = order['price']
    return dict(gross=gross, selected_coupon_id=None, coupon_id=None, coupon_name=None,
                coupon_discount=0, coupon_reason='NONE', coupon_message=REASONS['NONE'],
                points_requested=0, points_used=0, points_clipped=False, cash_due=gross,
                points_to_earn=0, policy='legacy_no_benefits', unit='KRW')


def hold(state: dict, pricing: dict) -> None:
    wallet = state.setdefault('wallet', initial_wallet())
    if pricing['points_used'] > wallet['balance']:
        raise ValueError('point hold exceeds balance')
    wallet.update(held_points=pricing['points_used'], held_coupon=pricing['coupon_id'])


def release(state: dict) -> None:
    wallet = state.setdefault('wallet', initial_wallet())
    wallet.update(held_points=0, held_coupon=None)


def consume(state: dict, pricing: dict) -> None:
    wallet = state.setdefault('wallet', initial_wallet())
    if wallet['held_points'] != pricing['points_used'] or wallet['held_coupon'] != pricing['coupon_id']:
        raise ValueError('benefit hold does not match approved quote')
    wallet['balance'] += pricing['points_to_earn'] - pricing['points_used']
    wallet['spent'] += pricing['points_used']
    wallet['earned'] += pricing['points_to_earn']
    if pricing['coupon_id']:
        if pricing['coupon_id'] in wallet['used_coupons']:
            raise ValueError('coupon already consumed')
        wallet['used_coupons'].append(pricing['coupon_id'])
    release(state)
