"""Deadline-aware plans over a synthetic walk graph; no live routing claims."""
from __future__ import annotations
from copy import deepcopy
from hashlib import sha256
import heapq
import json
from . import agreement
from .benefits import COUPONS, INITIAL_POINTS, price_quote, legacy_pricing

NODES = {
    'station': [9, 79], 'j1': [31, 76], 'j2': [51, 64], 'j3': [69, 43],
    'office': [87, 17], 'j4': [15, 46], 'park': [14, 17],
    'corner': [34, 60], 'wave': [51, 43], 'oat': [72, 24],
    'garden': [29, 34], 'express': [78, 69],
}
EDGES = [('station','j1',2),('j1','j2',2),('j2','j3',2),('j3','office',2),
         ('j1','corner',1),('corner','j2',2),('j2','wave',1),('wave','j3',1),
         ('j3','oat',1),('oat','office',2),('station','j4',3),('j4','park',2),
         ('j4','garden',1),('garden','j1',3),('j2','express',2),('express','office',4)]
STORES = [
    dict(id='corner', name='모퉁이 커피', subtitle='가장 가깝지만, 지금은 바쁜 곳',
         queue_until=7, prices={'americano':3300,'latte':4500}, prep=3, oat=True, decaf=True, online=True),
    dict(id='wave', name='웨이브 로스터스', subtitle='출근길에 들르기 좋은 로스터리',
         queue_until=1, prices={'americano':3100,'latte':4300}, prep=3, oat=True, decaf=False, online=True),
    dict(id='oat', name='오트 스튜디오', subtitle='회사 옆, 나에게 맞는 한 잔',
         queue_until=0, prices={'americano':3500,'latte':4700}, prep=4, oat=True, decaf=True, online=True),
    dict(id='garden', name='가든 브루', subtitle='공원 산책길의 작은 카페',
         queue_until=0, prices={'americano':4400,'latte':5900}, prep=3, oat=False, decaf=True, online=True),
    dict(id='express', name='에스프레소 바', subtitle='가볍게 들르는 커피 스탠드',
         queue_until=0, prices={'americano':2500}, prep=2, oat=False, decaf=False, online=True),
]
for _shop in STORES:
    _shop['transfer_policy'] = agreement.default_policy(_shop['id'])

REASONS = {'MENU':'선택한 메뉴가 없어요', 'MILK':'오트 변경이 어려워요',
           'DECAF':'디카페인을 제공하지 않아요', 'BUDGET':'예산을 초과해요',
           'DEADLINE':'도착 마감에 늦어요', 'DETOUR':'허용한 우회보다 멀어요',
           'OFFLINE':'지금 주문을 받지 않아요', **agreement.REASONS}

def digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(',',':'),ensure_ascii=False).encode()).hexdigest()

def shortest(source: str, destination: str) -> tuple[int,list[str]]:
    queue = [(0, source, [source])]; visited = set()
    while queue:
        distance, node, path = heapq.heappop(queue)
        if node == destination: return distance, path
        if node in visited: continue
        visited.add(node)
        for a,b,cost in EDGES:
            neighbour = b if a==node else a if b==node else None
            if neighbour and neighbour not in visited:
                heapq.heappush(queue,(distance+cost,neighbour,path+[neighbour]))
    raise ValueError('unknown or disconnected destination')

def catalogue() -> dict:
    return dict(nodes=NODES, edges=EDGES, stores=deepcopy(STORES),
                destinations=[dict(id='office',name='오피스 타워'),dict(id='park',name='센트럴 파크')],
                mode='synthetic', minutes_per_tick=1, start_time='08:40', safety_buffer=1,
                benefits=dict(initial_points=INITIAL_POINTS, coupons=deepcopy(COUPONS),
                              scope='journey_demo', policy='coupon_then_points_v1'))

def plans(state: dict) -> list[dict]:
    intent=state['intent']; now=state['clock']
    direct,_=shortest('station',intent['destination']); rows=[]
    for store in state['stores']:
        order=state.get('order')
        # The active reservation keeps its agreed departure, not a sliding ETA.
        # Alternatives conservatively start at the fixed origin at the current tick.
        depart=(order['departure_at'] if order and order['store_id']==store['id'] else now)+state['arrival_delay']
        walk_to,first=shortest('station',store['id'])
        walk_after,second=shortest(store['id'],intent['destination'])
        price=store['prices'].get(intent['drink'],0)
        price += 600 if intent['milk']=='oat' and intent['drink']=='latte' else 0
        price += 200 if intent['decaf'] else 0
        pricing = price_quote(state, store['id'], price)
        if order and order['store_id'] == store['id']:
            pricing = deepcopy(order.get('pricing', legacy_pricing(order)))
        price = pricing['cash_due']
        prep=store['prep'] + (1 if intent['drink']=='latte' else 0)
        start=max(now,store['queue_until'],depart+walk_to-prep)
        ready=start+prep; pickup=max(depart+walk_to,ready)
        arrival=pickup+walk_after; detour=walk_to+walk_after-direct
        reasons=[]
        if intent['drink'] not in store['prices']: reasons.append('MENU')
        if intent['milk']=='oat' and not store['oat']: reasons.append('MILK')
        if intent['decaf'] and not store['decaf']: reasons.append('DECAF')
        if not store['online']: reasons.append('OFFLINE')
        if price>intent['budget']: reasons.append('BUDGET')
        if detour>intent['max_detour']: reasons.append('DETOUR')
        if arrival+1>intent['deadline_minutes']: reasons.append('DEADLINE')
        terms = agreement.assess(state,store,pricing) if state.get('transfer_agreement_version') == 1 else None
        if terms:
            reasons.extend(terms['reason_codes'])
        row=dict(store_id=store['id'], name=store['name'], subtitle=store['subtitle'],
                 walk_to=walk_to, walk_after=walk_after, walk_total=walk_to+walk_after,
                 detour=detour, wait=max(0,ready-depart-walk_to), price=price, pricing=pricing,
                 start_at=start,ready_at=ready,pickup_at=pickup,arrival_at=arrival,
                 margin=intent['deadline_minutes']-arrival,prep=prep,
                 queue=max(0,store['queue_until']-now),milk=intent['milk'],decaf=intent['decaf'],
                 route=first+second[1:],first_leg=first,second_leg=second,
                 feasible=not reasons,reasons=reasons,reason_labels=[REASONS[r] for r in reasons])
        if terms:
            row['transfer_terms'] = terms
        row['quote_id']=digest({'journey':state['id'],'version':state['version'],'plan':row})[:32]
        rows.append(row)
    key = {'arrival':lambda p:(p['arrival_at'],p['price'],p['walk_total']),
           'price':lambda p:(p['price'],p['arrival_at'],p['walk_total']),
           'walk':lambda p:(p['walk_total'],p['arrival_at'],p['price'])}[intent['priority']]
    return sorted(rows,key=lambda p:(not p['feasible'],key(p),p['store_id']))
