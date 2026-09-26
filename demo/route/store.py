"""One persisted journey across customer, merchant, rescue and receipt views.

Pricing and benefits finalize in the coordinator DB; modeled merchants commit
to independent files through durable operations. No live merchant API is used.
"""
from __future__ import annotations
from contextlib import contextmanager
from copy import deepcopy
import hmac
import json
from pathlib import Path
import secrets
import sqlite3
from uuid import uuid4
from .planner import STORES, REASONS, digest, plans
from . import benefits
from .outcomes import compare_order, current_plan

class Conflict(ValueError):
    def __init__(self,code,message): super().__init__(message); self.code=code

def require(condition,code,message):
    if not condition: raise Conflict(code,message)

class JourneyStore:
    def __init__(self,path, fleet=None):
        self.automatic_recovery_enabled = False
        self.path=str(path); Path(path).parent.mkdir(parents=True,exist_ok=True)
        with self.connection() as db:
            db.executescript('''PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS route_journeys(id TEXT PRIMARY KEY, body TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS route_commands(journey_id TEXT NOT NULL,
                request_id TEXT NOT NULL,fingerprint TEXT NOT NULL,
                PRIMARY KEY(journey_id,request_id));
              CREATE TABLE IF NOT EXISTS route_controls(journey_id TEXT NOT NULL,
                request_id TEXT NOT NULL,fingerprint TEXT NOT NULL,
                PRIMARY KEY(journey_id,request_id));''')
        from .merchant_fleet import MerchantFleet
        from .durable_operations import DurableOperations
        self.fleet = fleet or MerchantFleet(self.path + '.merchants')
        self.operations = DurableOperations(self)

    @contextmanager
    def connection(self):
        db=sqlite3.connect(self.path,timeout=15,isolation_level=None)
        try: yield db
        finally: db.close()

    def _load(self,db,sid):
        row=db.execute('SELECT body FROM route_journeys WHERE id=?',(sid,)).fetchone()
        if row is None: raise KeyError(sid)
        return json.loads(row[0])

    def _save(self,db,s):
        db.execute('INSERT INTO route_journeys VALUES(?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body',
                   (s['id'],json.dumps(s,ensure_ascii=False,separators=(',',':'))))

    @staticmethod
    def event(s,kind,title,**data):
        s['events'].append(dict(seq=len(s['events'])+1,type=kind,title=title,at=s['clock'],data=data))

    def create(self,intent,world_id=None):
        s=dict(id=uuid4().hex,version=1,clock=0,arrival_delay=0,intent=intent,
               stores=deepcopy(STORES),order=None,events=[],mode='synthetic',wallet=benefits.initial_wallet(),transfer_agreement_version=1)
        s['world_id'] = world_id or s['id']
        self.event(s,'INTENT_CREATED','일정과 커피 조건을 저장했어요',intent=intent)
        with self.connection() as db: self._save(db,s)
        return self.view(s)

    def get(self,sid):
        with self.connection() as db: return self.view(self._load(db,sid))

    def view(self,s,duplicate=False):
        out=deepcopy(s); rows=plans(s); order=out['order']; current=None
        out['automatic_recovery_enabled'] = self.automatic_recovery_enabled
        if order:
            current=next(p for p in rows if p['store_id']==order['store_id'])
            if order['state'] in {'PREPARING','READY','PICKED_UP'}:
                current=deepcopy(current)
                current['ready_at']=order['ready_at']
                current['pickup_at']=(order.get('picked_up_at',order['ready_at']) if order['state']=='PICKED_UP'
                    else max(s['clock'],order['departure_at']+s['arrival_delay']+current['walk_to'],order['ready_at']))
                current['arrival_at']=current['pickup_at']+current['walk_after']
                current['margin']=s['intent']['deadline_minutes']-current['arrival_at']
                current['reasons']=[r for r in current['reasons'] if r not in {'OFFLINE','MENU','DEADLINE'}]
                if current['margin']<1: current['reasons'].append('DEADLINE')
                current['feasible']=not current['reasons']
                current['reason_labels']=[REASONS[r] for r in current['reasons']]
                rows=[current if p['store_id']==order['store_id'] else p for p in rows]
            if order['state'] not in {'READY','PICKED_UP'}: order['pickup_code']=None
        active=order and order['state'] not in {'CANCELLED','PICKED_UP'}
        risk=bool(active and current and not current['feasible'])
        recommendations=[p for p in rows if p['feasible']]
        if order and order['state']=='RESERVED':
            recommendations=[p for p in recommendations if p['store_id']!=order['store_id']]
        if order and order['state']!='RESERVED': recommendations=[]
        capture=[e for e in s['events'] if e['type']=='PAYMENT_CAPTURED']
        receipt=dict(order_id=order['id'] if order else None,
                     capture_count=len(capture),net_paid=sum(e['data']['amount'] for e in capture),
                     payable=order['price'] if active else 0,
                     authorization_count=sum(e['type']=='PAYMENT_AUTHORIZED' for e in s['events']),
                     transfers=[e for e in s['events'] if e['type']=='ORDER_TRANSFERRED'])
        settlement=[e['data'] for e in s['events'] if e['type']=='MERCHANT_SETTLEMENT_SIMULATED' and (not order or e['data'].get('order_id')==order['id'])]
        receipt['settlement']=deepcopy(settlement[-1]) if settlement else None
        pricing=order.get('pricing', benefits.legacy_pricing(order)) if order else None
        receipt.update(pricing=pricing, coupon_discount=pricing['coupon_discount'] if pricing else 0,
                       points_spent=benefits.wallet_for(s)['spent'], points_earned=benefits.wallet_for(s)['earned'])
        out['wallet']=benefits.wallet_view(s)
        out['comparison']=compare_order(s)
        out.update(all_plans=rows,recommendations=recommendations,current_plan=current,
                   risk=dict(needs_attention=risk,can_transfer=bool(order and order['state']=='RESERVED'),
                       reasons=current['reasons'] if risk else []), receipt=receipt, duplicate=duplicate)
        if 'world_id' in s:
            try:
                out['merchant_capacity'] = self.fleet.snapshot(s['world_id'])
                out['merchant_connection'] = 'available'
            except OSError:
                out['merchant_capacity'] = {}
                out['merchant_connection'] = 'unavailable'
            out['handoff'] = deepcopy(s.get('handoff'))
            out['handoff_pending'] = bool(s.get('active_operation'))
            if out['handoff_pending']:
                out['recommendations'] = []
                out['risk']['can_transfer'] = False
        return out

    def command(self,sid,c):
        result = self.operations.command(sid,c)
        return result if result is not None else self._command_local(sid,c)

    def _command_local(self,sid,c):
        fp=digest(c)
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                s=self._load(db,sid)
                previous=db.execute('SELECT fingerprint FROM route_commands WHERE journey_id=? AND request_id=?',
                                    (sid,c['request_id'])).fetchone()
                if previous:
                    require(previous[0]==fp,'IDEMPOTENCY_CONFLICT','같은 요청 번호로 다른 작업을 보낼 수 없어요.')
                    db.execute('COMMIT'); return self.view(s,True)
                require(s['version']==c['expected_version'],'STALE_VERSION','상태가 바뀌었어요. 새로 확인한 뒤 다시 선택해 주세요.')
                require(not s.get('active_operation'),'TRANSFER_PENDING','매장 확인이 끝나지 않았어요. 먼저 다시 확인해 주세요.')
                self._apply(s,c)
                s['version']+=1
                self._save(db,s)
                db.execute('INSERT INTO route_commands VALUES(?,?,?)',(sid,c['request_id'],fp))
                db.execute('COMMIT')
                return self.view(s)
            except Exception:
                if db.in_transaction: db.execute('ROLLBACK')
                raise

    def _quote(self,s,qid):
        found=next((p for p in plans(s) if p['quote_id']==qid),None)
        require(found is not None,'STALE_QUOTE','이 경로는 더 이상 유효하지 않아요. 최신 경로를 골라 주세요.')
        require(found['feasible'],'INFEASIBLE','시간·예산·메뉴 조건을 만족하지 않는 경로예요.')
        return found

    def _apply(self,s,c):
        action=c['action']; order=s['order']
        if action=='reserve_first_reorder':
            # Strong comparison policy, not exposed as a customer API action.
            # The same persisted handoff protocol protects both orders. Only
            # after acceptance does the candidate cancel/requalify/re-authorize.
            require(order is not None and order['state']=='RESERVED','ALREADY_PREPARING','제조 전 주문만 새 자리를 확보해 다시 주문해요.')
            chosen=self._quote(s,c.get('quote_id'))
            require(chosen['store_id']!=order['store_id'],'SAME_STORE','다른 매장을 선택해 주세요.')
            target=chosen['store_id']
            self._apply(s,{'action':'cancel'})
            s.setdefault('previous_orders',[]).append(deepcopy(s['order']))
            s['order']=None
            fresh=next(p for p in plans(s) if p['store_id']==target)
            require(fresh['feasible'],'REORDER_PREFLIGHT_REJECTED','새 주문에 적용할 조건을 만족하지 않아요.')
            require(type(c.get('accepted_cash_due')) is int and fresh['price']==c['accepted_cash_due'],
                    'REORDER_PRICE_CHANGED','새 주문의 결제 금액을 먼저 확인해야 해요.')
            self._apply(s,{'action':'reserve','quote_id':fresh['quote_id']})
            return
        if action=='reorder':
            require(order is not None and order['state']=='CANCELLED','INVALID_STATE','먼저 기존 주문을 취소해야 다시 주문할 수 있어요.')
            s.setdefault('previous_orders',[]).append(deepcopy(order))
            s['order']=None
            return self._apply(s,{**c,'action':'reserve'})
        if action=='reserve':
            require(order is None,'ORDER_EXISTS','이미 주문한 일정이에요. 새 결제 대신 주문을 옮길 수 있어요.')
            p=self._quote(s,c.get('quote_id'))
            s['order']=dict(id='PCT-'+uuid4().hex[:10].upper(),store_id=p['store_id'],
                store_name=p['name'],state='RESERVED',price=p['price'],pricing=deepcopy(p['pricing']),plan=p,ready_at=p['ready_at'],departure_at=s['clock'],
                pickup_code=f'{secrets.randbelow(1_000_000):06d}',original_store=p['name'])
            if p.get('transfer_terms'):
                s['order']['commercial_terms']=deepcopy(p['transfer_terms'])
            benefits.hold(s,p['pricing'])
            self.event(s,'BENEFITS_HELD','쿠폰·포인트 사용을 보류했어요',pricing=p['pricing'])
            self.event(s,'PAYMENT_AUTHORIZED','모의 결제 승인 1건',amount=p['price'])
            self.event(s,'ORDER_RESERVED',p['name']+'에 주문했어요',store_id=p['store_id'],amount=p['price'])
            return
        if action=='disrupt':
            store=next((v for v in s['stores'] if v['id']==c.get('store_id')),None)
            require(store is not None,'UNKNOWN_STORE','매장을 확인해 주세요.')
            minutes=c.get('minutes',0)
            require(minutes>0,'BAD_DELAY','혼잡 증가 시간은 1분 이상이어야 해요.')
            store['queue_until']=max(store['queue_until'],s['clock'])+minutes
            if order and order['store_id']==store['id'] and order['state']=='PREPARING':
                order['ready_at']+=minutes
            self.event(s,'STORE_DELAYED',store['name']+f' 준비가 {minutes}분 늦어져요',store_id=store['id'],minutes=minutes)
            return
        if action=='delay':
            require(not order or order['state'] not in {'CANCELLED','PICKED_UP'},'TERMINAL','이미 끝난 주문이에요.')
            s['arrival_delay']+=c.get('minutes',0)
            if order and order['state']=='RESERVED':
                p=next(p for p in plans(s) if p['store_id']==order['store_id'])
                order['plan']=p; order['ready_at']=p['ready_at']
            self.event(s,'ARRIVAL_UPDATED','출발이 늦어진 상황을 반영했어요',minutes=c.get('minutes',0))
            return
        if action=='advance':
            require(s['clock']+c.get('minutes',0)<=180,'CLOCK_LIMIT','가상 체험 시간은 3시간 이내예요.')
            s['clock']+=c.get('minutes',0)
            self.event(s,'CLOCK_ADVANCED',f"체험 시계를 {c.get('minutes',0)}분 진행했어요")
            return
        require(order is not None,'NO_ORDER','먼저 주문을 선택해 주세요.')
        if action=='transfer':
            require(order['state']=='RESERVED','ALREADY_PREPARING','제조가 시작된 주문은 다른 매장으로 옮기지 않아요.')
            p=self._quote(s,c.get('quote_id'))
            require(order['store_id']!=p['store_id'],'SAME_STORE','다른 매장을 선택해 주세요.')
            old_name=order['store_name']; old_price=order['price']; old_store=order['store_id']
            old_pricing=deepcopy(order.get('pricing',benefits.legacy_pricing(order)))
            old_arrival=current_plan(s)['arrival_at']
            benefits.hold(s,p['pricing'])
            order.update(store_id=p['store_id'],store_name=p['name'],price=p['price'],pricing=deepcopy(p['pricing']),plan=p,ready_at=p['ready_at'],departure_at=s['clock'])
            if p.get('transfer_terms'):
                order['commercial_terms']=deepcopy(p['transfer_terms'])
            self.event(s,'ORDER_TRANSFERRED',old_name+' → '+p['name'],
                       accepted_terms=deepcopy(p.get('transfer_terms')),
                       from_store=old_store,to_store=p['store_id'],from_name=old_name,to_name=p['name'],
                       old_price=old_price,new_price=p['price'],difference=p['price']-old_price,order_id=order['id'],
                       old_pricing=old_pricing,new_pricing=p['pricing'],
                       old_arrival=old_arrival,new_arrival=p['arrival_at'])
            self.event(s,'AUTHORIZATION_ADJUSTED','모의 승인 금액만 조정했어요',amount=p['price'],difference=p['price']-old_price)
        elif action=='start':
            require(order['state']=='RESERVED','INVALID_STATE','제조 전 주문만 시작할 수 있어요.')
            p=next(p for p in plans(s) if p['store_id']==order['store_id'])
            require(s['clock']>=p['start_at'],'TOO_EARLY','아직 제조 시작 시각 전이에요. 체험 시계를 진행해 주세요.')
            require(p['feasible'],'UNSAFE_START','현재 시간 조건을 지킬 수 없어요. 고객에게 변경을 제안해 주세요.')
            order.update(state='PREPARING',ready_at=s['clock']+p['prep'])
            self.event(s,'PREPARATION_STARTED',order['store_name']+'에서 제조를 시작했어요',store_id=order['store_id'])
        elif action=='ready':
            require(order['state']=='PREPARING','INVALID_STATE','제조 중인 주문만 준비 완료로 바뀔 수 있어요.')
            require(s['clock']>=order['ready_at'],'TOO_EARLY','음료 준비 시간이 아직 남아 있어요.')
            order['state']='READY'
            self.event(s,'ORDER_READY','커피가 준비됐어요. 수령 코드를 확인해 주세요.')
        elif action=='claim':
            require(order['state'] in {'READY','PICKED_UP'},'NOT_READY','아직 수령 가능한 주문이 아니에요.')
            require(hmac.compare_digest(str(c.get('pickup_code','')).encode('utf-8'),order['pickup_code'].encode('utf-8')),'BAD_CODE','수령 코드가 맞지 않아요.')
            if order['state']=='PICKED_UP': return
            pricing=order.get('pricing',benefits.legacy_pricing(order))
            benefits.consume(s,pricing)
            self.event(s,'BENEFITS_CONSUMED','쿠폰·포인트 사용과 적립을 확정했어요',
                       coupon_id=pricing['coupon_id'],points_used=pricing['points_used'],points_earned=pricing['points_to_earn'])
            order['state']='PICKED_UP'
            order['picked_up_at']=s['clock']
            self.event(s,'PICKUP_COMPLETED','같은 주문으로 커피를 받았어요')
            self.event(s,'PAYMENT_CAPTURED','모의 결제를 한 번 확정했어요',amount=order['price'])
            if order.get('commercial_terms'):
                from .agreement import funding
                self.event(s,'MERCHANT_SETTLEMENT_SIMULATED','수령 매장의 모의 수취액을 기록했어요',
                           order_id=order['id'],**funding(order['store_id'],pricing))
        elif action=='cancel':
            if order['state']=='CANCELLED': return
            require(order['state']=='RESERVED','ALREADY_PREPARING','제조 시작 후에는 자동 취소하지 않아요.')
            pricing=order.get('pricing',benefits.legacy_pricing(order))
            benefits.release(s)
            self.event(s,'BENEFITS_RELEASED','보류 중인 쿠폰·포인트를 돌려드렸어요',
                       coupon_id=pricing['coupon_id'],points=pricing['points_used'])
            order['state']='CANCELLED'
            self.event(s,'ORDER_CANCELLED','주문 취소 · 모의 승인 해제',amount=order['price'])
        else:
            raise Conflict('INVALID_COMMAND','지원하지 않는 명령이에요.')

    def transfer_control(self,sid,c):
        """Bounded demo controls; never mutate another visitor's world."""
        if c['action'] in {'occupy','clear'}:
            return self.operations.control(sid,c)
        from .durable_operations import FAULTS
        from .merchant_fleet import CAPACITY
        fp=digest(c)
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                s=self._load(db,sid)
                require('world_id' in s,'LEGACY_JOURNEY','새 체험에서 매장 변경을 확인해 주세요.')
                previous=db.execute('SELECT fingerprint FROM route_controls WHERE journey_id=? AND request_id=?',(sid,c['request_id'])).fetchone()
                if previous:
                    require(previous[0]==fp,'IDEMPOTENCY_CONFLICT','이미 보낸 설정과 내용이 달라요.')
                    db.execute('COMMIT')
                    return self.view(s,True)
                require(s['version']==c['expected_version'],'STALE_VERSION','주문 상태를 다시 확인해 주세요.')
                require(not s.get('active_operation'),'TRANSFER_PENDING','먼저 매장의 처리 결과를 확인해 주세요.')
                if c['action']=='fault':
                    require(c.get('fault') in FAULTS,'INVALID_COMMAND','지원하지 않는 체험 설정이에요.')
                    s['next_transfer_fault']=c['fault']
                else:
                    require(False,'INVALID_COMMAND','지원하지 않는 체험 설정이에요.')
                s['version']+=1
                self._save(db,s)
                db.execute('INSERT INTO route_controls VALUES(?,?,?)',(sid,c['request_id'],fp))
                db.execute('COMMIT')
                return self.view(s)
            except BaseException:
                if db.in_transaction: db.execute('ROLLBACK')
                raise
