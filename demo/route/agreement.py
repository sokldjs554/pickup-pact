"""Versioned, explicitly synthetic partner and substitution agreement.

A quote is the acceptance token for these terms. This is a proposed policy,
not evidence that real merchants have agreed or that two coffees taste alike.
"""
from __future__ import annotations
from hashlib import sha256
import json

VERSION = 'partner-transfer-v1'
REASONS = {
    'PARTNER_DISABLED': '매장 변경에 참여하지 않는 매장이에요',
    'PARTNER_GROUP': '서로 주문을 넘겨받는 매장 그룹이 아니에요',
    'PARTNER_UNVERIFIED': '매장 간 변경 조건이 확인되지 않았어요',
    'PRODUCT_SPEC': '음료 용량이나 온도가 달라 같은 조건으로 바꿀 수 없어요',
}


def default_policy(shop: str) -> dict:
    return dict(enabled=shop not in {'garden','express'}, group='commute-demo',
                revision=1, volume_ml=360, temperature='ICED')


def funding(shop: str, pricing: dict) -> dict:
    """KRW funding for this modeled sale. Issued loyalty points are separate."""
    gross, discount, points, cash = (pricing[k] for k in
        ('gross','coupon_discount','points_used','cash_due'))
    if any(type(v) is not int or v < 0 for v in (gross,discount,points,cash)):
        raise ValueError('funding requires non-negative integer KRW amounts')
    if cash + discount + points != gross:
        raise ValueError('funding does not balance with approved menu amount')
    if discount and pricing.get('coupon_id') not in {'welcome500','wave1000','morning10'}:
        raise ValueError('coupon funding policy is unknown')
    store_coupon = discount if pricing.get('coupon_id')=='wave1000' else 0
    if store_coupon and shop!='wave':
        raise ValueError('store-funded coupon cannot be charged to a different shop')
    return dict(mode='synthetic',unit='KRW',merchant_store_id=shop,
                gross=gross,customer_cash=cash,merchant_coupon=store_coupon,
                platform_coupon=discount-store_coupon,platform_points=points,
                merchant_receivable=gross-store_coupon,
                platform_support=discount-store_coupon+points,
                liability_policy='platform_funds_global_coupon_and_redeemed_points_v1')


def _profile(shop: dict, intent: dict) -> dict:
    policy=shop.get('transfer_policy',{})
    return dict(drink=intent['drink'],milk=intent['milk'],decaf=intent['decaf'],
                volume_ml=policy.get('volume_ml'),temperature=policy.get('temperature'))


def assess(state: dict, target: dict, pricing: dict) -> dict:
    order=state.get('order')
    active=bool(order and order['state'] not in {'CANCELLED','PICKED_UP'})
    source=next((s for s in state['stores'] if active and s['id']==order['store_id']),None)
    source_product = ((order.get('commercial_terms') or {}).get('target_product') or _profile(source,state['intent'])) if source else None
    reasons=[]
    is_transfer=bool(source and source['id']!=target['id'])
    if is_transfer:
        policies=[s.get('transfer_policy',{}) for s in (source,target)]
        required={'enabled','group','revision','volume_ml','temperature'}
        if any(not required <= p.keys() or not isinstance(p['group'],str) or not p['group']
               or type(p['revision']) is not int or p['revision']<1
               or type(p['volume_ml']) is not int or p['volume_ml']<=0
               or p['temperature'] not in {'ICED','HOT'} for p in policies):
            reasons.append('PARTNER_UNVERIFIED')
        else:
            if any(p['enabled'] is not True for p in policies): reasons.append('PARTNER_DISABLED')
            if policies[0]['group']!=policies[1]['group']: reasons.append('PARTNER_GROUP')
            if source_product!=_profile(target,state['intent']): reasons.append('PRODUCT_SPEC')
    value=dict(version=VERSION,source_store_id=source['id'] if source else None,
               target_store_id=target['id'],eligible=not reasons,reason_codes=reasons,
               source_policy=source.get('transfer_policy') if source else None,
               target_policy=target.get('transfer_policy'),
               source_product=source_product,
               target_product=_profile(target,state['intent']),funding=funding(target['id'],pricing),
               notice='용량·온도·선택 옵션을 맞춰요. 원두와 맛은 매장마다 달라요. 바뀌는 메뉴와 금액을 보고 결정해 주세요.',
               merchant_rule='가상 참여 매장의 제조 전 주문만 변경해요. 기존 매장 수취액은 0원이며 새 매장이 수령 완료분을 받아요.',
               confirmation='현재 견적의 확인 버튼을 누르면 이 조건으로 변경을 요청해요. 매장 수락과 제조 상태는 처리할 때 다시 확인해요.')
    value['terms_id']=sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return value
