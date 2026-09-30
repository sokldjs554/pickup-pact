"""V2 coordinator: durable plans, unlocked I/O, then revision-fenced evidence.

The old protocol is retained for existing journeys and historical comparisons.
Only the synthetic PG can attest financial effects. A candidate is a plan, not
success; the provider transaction ID is attached after an authenticated reply.
"""
from __future__ import annotations
from copy import deepcopy
import json
import sqlite3
import time
from uuid import uuid4
from .planner import digest
from .payment_steps import TEXT, PAY_PHASES, FAULTS, public, expire, transition, auth_command, transport_fault
from .payments.domain import PaymentError
from .payments.notifications import PaymentInbox
from .recovery_worker import initial_schedule, after_step

ACTIONS={'reserve','reorder','transfer','start','ready','claim','cancel'}


class PaymentOperations:
    def __init__(self,store):
        self.store=store

    def command(self,sid,c):
        from .store import require
        fp=digest(c);replay=False;local=False
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            s=self.store._load(db,sid)
            old=db.execute('SELECT id,fingerprint FROM route_operations WHERE journey_id=? AND request_id=?',(sid,c['request_id'])).fetchone()
            local_old=db.execute('SELECT fingerprint FROM route_commands WHERE journey_id=? AND request_id=?',(sid,c['request_id'])).fetchone()
            if old:
                require(old[1]==fp,'IDEMPOTENCY_CONFLICT','같은 요청 번호의 내용이 달라요.')
                oid=old[0];replay=True
            elif local_old:
                require(local_old[0]==fp,'IDEMPOTENCY_CONFLICT','같은 요청 번호의 내용이 달라요.')
                db.execute('COMMIT');return self.store.view(s,True)
            else:
                require(s['version']==c['expected_version'],'STALE_VERSION','주문 상태를 다시 확인해 주세요.')
                if c['action']=='recover':
                    oid=s.get('active_operation')
                    if oid:
                        op=self.store.operations._op(db,oid)
                        if op.get('protocol_version')==2:
                            if op['recovery']['state']=='REVIEW_REQUIRED':
                                require(op.get('manual_reviews',0)<3,'REVIEW_LIMIT','이 작업은 추가 운영 확인이 필요해요.')
                                op['manual_reviews']=op.get('manual_reviews',0)+1
                                op['recovery'].update(state='SCHEDULED',retry_count=0,next_retry_at=time.time())
                                op['phase_version']+=1
                                self.store.operations._save_op(db,op)
                    db.execute('INSERT INTO route_commands VALUES(?,?,?)',(sid,c['request_id'],fp))
                    db.execute('COMMIT')
                    return self.resume(sid,oid) if oid else self.store.view(s)
                require(not s.get('active_operation'),'TRANSFER_PENDING','매장·결제 확인이 끝나지 않았어요. 다시 결제하지 마세요.')
                if c['action'] not in ACTIONS:
                    local=True;oid=None
                else:
                    candidate=deepcopy(s)
                    self.store._apply(candidate,c)
                    if c['action'] in {'claim','cancel'} and candidate['order']==s['order']:
                        db.execute('INSERT INTO route_commands VALUES(?,?,?)',(sid,c['request_id'],fp))
                        db.execute('COMMIT');return self.store.view(s,True)
                    op=self._plan(s,c,candidate,fp)
                    oid=op['id']
                    for auth in (op['new_auth'],op['old_auth']):
                        if auth:PaymentInbox.register_in(db,auth['world_id'],auth['order_id'],auth['authorization_id'])
                    db.execute('INSERT INTO route_operations VALUES(?,?,?,?,?)',
                               (oid,sid,c['request_id'],fp,json.dumps(op,ensure_ascii=False)))
                    s['active_operation']=oid;s['pending_order_id']=op['order_id'];s['payment_order_id']=op['order_id']
                    s['handoff']=public(op);s['version']+=1
                    self.store._save(db,s)
            db.execute('COMMIT')
        if local:return self.store._command_local(sid,c)
        return self.resume(sid,oid,duplicate=replay)

    def _plan(self,s,c,candidate,fp):
        before=s.get('order');after=candidate['order'];action=c['action'];oid=uuid4().hex
        gen=(before or {}).get('merchant_generation',0)
        if action=='transfer':
            gen=max(gen,s.get('merchant_generation_counter',0))+1
            s['merchant_generation_counter']=candidate['merchant_generation_counter']=gen
        elif action in {'reserve','reorder'}:gen=0
        after['merchant_generation']=gen
        old_auth=deepcopy(s.get('payment',{}).get('authorization'))
        new_auth=old_auth
        if action in {'reserve','reorder','transfer'}:
            if action!='transfer' or before['price']!=after['price']:
                rev=s.get('payment_revision_counter',0)+1
                s['payment_revision_counter']=candidate['payment_revision_counter']=rev
                new_auth=(dict(authorization_id='AUTH-'+uuid4().hex,world_id=s['world_id'],order_id=after['id'],
                               amount_krw=after['price'],currency='KRW',payment_revision=rev,
                               quote_fingerprint=digest({'quote_id':after['plan']['quote_id'],'pricing':after['pricing'],'terms':after.get('commercial_terms')} )) if after['price'] else None)
        phase={'reserve':'PAY_AUTHORIZE' if new_auth else 'ADMIT',
               'reorder':'PAY_AUTHORIZE' if new_auth else 'ADMIT','transfer':'FREEZE',
               'claim':'PAY_CAPTURE' if old_auth else 'CLAIM',
               'cancel':'CANCEL','start':'PAY_CHECK' if old_auth else 'START',
               'ready':'PAY_CHECK' if old_auth else 'READY'}[action]
        payment_fault=s.get('next_payment_fault','none')
        consumes = ((payment_fault=='authorize_reply_lost' and action in {'reserve','reorder','transfer'} and new_auth and new_auth!=old_auth)
                    or (payment_fault=='capture_reply_lost' and action=='claim' and old_auth)
                    or (payment_fault=='void_reply_lost' and old_auth and (action=='cancel' or action=='transfer' and new_auth!=old_auth))
                    or (payment_fault in {'notification_duplicate','notification_late'} and action in {'reserve','reorder','transfer','claim','cancel'}))
        if consumes:s['next_payment_fault']=candidate['next_payment_fault']='none'
        else:payment_fault='none'
        merchant_fault=s.get('next_transfer_fault','none') if action=='transfer' else 'none'
        if action=='transfer':s['next_transfer_fault']=candidate['next_transfer_fault']='none'
        return dict(id=oid,sid=s['id'],protocol_version=2,action=action,phase=phase,phase_version=0,
            status='PENDING',decision='UNDECIDED',message=TEXT[phase],failure=None,history=[],
            source=(before or {}).get('store_id'),target=after['store_id'],attempts=0,
            candidate=candidate,candidate_event_base=len(s['events']),world=s['world_id'],order_id=after['id'],
            source_order_id=(before or {}).get('id'),before_generation=(before or {}).get('merchant_generation',0),
            generation=gen,request_id=c['request_id'],fingerprint=fp,fault=merchant_fault,fault_consumed=False,
            payment_fault=payment_fault,old_auth=old_auth,new_auth=new_auth,
            card_token=s.get('payment_card','demo-approved'),results={},recovery=initial_schedule(time.time()))

    def control(self,sid,c):
        from .store import require, Conflict
        action=c.get('action')
        fields={'action','expected_version','request_id'}|({'fault'} if action=='fault' else {'card_token'})
        require(action in {'fault','card'} and set(c)==fields,'INVALID_PAYMENT_CONTROL','가상 결제 설정을 확인해 주세요.')
        if action=='fault':require(c['fault'] in FAULTS,'INVALID_PAYMENT_FAULT','지원하지 않는 결제 상황이에요.')
        else:require(c['card_token'] in {'demo-approved','demo-declined'},'INVALID_TEST_CARD','가상 카드만 선택할 수 있어요.')
        fp=digest(c);rid='payment:'+c['request_id']
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE');s=self.store._load(db,sid)
            require(s.get('protocol_version')==2,'LEGACY_JOURNEY','이전 주문은 기존 방식으로 유지해요. 새 체험에서 확인해 주세요.')
            old=db.execute('SELECT fingerprint FROM route_controls WHERE journey_id=? AND request_id=?',(sid,rid)).fetchone()
            if old:
                require(old[0]==fp,'IDEMPOTENCY_CONFLICT','이미 보낸 설정과 내용이 달라요.')
            else:
                require(s['version']==c['expected_version'],'STALE_VERSION','주문 상태를 다시 확인해 주세요.')
                require(not s.get('active_operation'),'TRANSFER_PENDING','진행 중인 작업을 먼저 확인해 주세요.')
                if action=='card':require(s['order'] is None,'PAYMENT_CARD_LOCKED','주문 후에는 가상 카드를 바꾸지 않아요.')
                s['next_payment_fault' if action=='fault' else 'payment_card']=c['fault' if action=='fault' else 'card_token']
                s['version']+=1;self.store._save(db,s)
                db.execute('INSERT INTO route_controls VALUES(?,?,?)',(sid,rid,fp))
            db.execute('COMMIT')
        return self.store.view(s,bool(old))

    def _prepare(self,sid,oid,automatic,now):
        from .store import require
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            s=self.store._load(db,sid);op=self.store.operations._op(db,oid)
            require(op['sid']==sid,'UNKNOWN_OPERATION','이 주문의 작업이 아니에요.')
            if op['status']!='PENDING':db.execute('COMMIT');return None
            require(s.get('active_operation')==oid,'OPERATION_FENCE','이미 다른 작업으로 바뀌었어요.')
            if op['recovery']['state']=='REVIEW_REQUIRED' or (automatic and op['recovery']['next_retry_at']>now):
                db.execute('COMMIT');return None
            if expire(op,now):
                op['phase_version']+=1;s['version']+=1;s['handoff']=public(op)
                self.store.operations._save_op(db,op);self.store._save(db,s)
            db.execute('COMMIT')
        return op

    def _external(self,op):
        phase=op['phase']
        if phase in {'DECIDE','FINALIZE'}:return {'ok':True}
        if phase in PAY_PHASES:
            if self.store.payment_gateway is None:raise OSError('payment gateway not connected')
            c=auth_command(op,phase)
            result=self.store.payment_gateway.operation(c)
            if result is None:result=self.store.payment_gateway.execute(c,fault=transport_fault(op,c))
            return result
        if phase=='PAY_CHECK':
            if self.store.payment_gateway is None:raise OSError('payment gateway not connected')
            auth=op['old_auth'];snapshot=self.store.payment_gateway.snapshot(op['world'],op['order_id'])
            found=next((a for a in snapshot['authorizations'] if a['authorization_id']==auth['authorization_id']),None)
            return {'ok':bool(found and found['status']=='AUTHORIZED' and all(found.get(k)==v for k,v in auth.items())),
                    'code':'AUTHORIZATION_NOT_ACTIVE','revision':snapshot['revision']}
        return self.store.operations._merchant(op,phase)

    def _observe(self,s,op,phase,result):
        if phase not in PAY_PHASES or not result.get('ok'):return
        if not result.get('transaction_id'):raise ValueError('missing provider transaction')
        seen=any(e['data'].get('transaction_id')==result['transaction_id'] for e in s['events'])
        if not seen:
            kind,title={'AUTHORIZE':('PAYMENT_AUTHORIZED','독립 가상 결제 서버의 승인을 확인했어요'),
                        'CAPTURE':('PAYMENT_CAPTURED','독립 가상 결제 서버의 청구를 확인했어요'),
                        'VOID':('PAYMENT_VOIDED','독립 가상 결제 서버의 승인 해제를 확인했어요')}[result['action']]
            self.store.event(s,kind,title,amount=result['amount_krw'],transaction_id=result['transaction_id'],
                             authorization_id=result['authorization_id'],payment_revision=result['payment_revision'],
                             provider_revision=result['revision'],provider='DEMO_PLATFORM',mode='synthetic')
        s['payment']['last_confirmed']={k:result[k] for k in ('transaction_id','authorization_id','outcome','amount_krw','revision')}

    def _finalize(self,s,op):
        new=deepcopy(op['candidate'])
        domain_events=new['events'][op['candidate_event_base']:]
        new['events']=deepcopy(s['events'])
        for event in domain_events:
            event=deepcopy(event);event['seq']=len(new['events'])+1;new['events'].append(event)
        # The current observation and notification tables must not be overwritten
        # by the older candidate prepared before external I/O.
        new['payment']=deepcopy(s['payment'])
        auth=op['new_auth'] if op['action'] in {'reserve','reorder','transfer'} else op['old_auth']
        state=('NO_CHARGE' if new['order']['price']==0 else
               'CAPTURED' if new['order']['state']=='PICKED_UP' else
               'VOIDED' if new['order']['state']=='CANCELLED' else 'AUTHORIZED')
        new['payment'].update(state=state,authorization=deepcopy(auth),amount_krw=new['order']['price'])
        new['payment_order_id']=op['order_id']
        new.setdefault('first_order_id',op['order_id'])
        return new

    def resume(self,sid,oid,duplicate=False,automatic=False,now=None):
        if oid is None:return self.store.get(sid)
        for iteration in range(16):
            tick=time.time() if now is None else now
            op=self._prepare(sid,oid,automatic and iteration==0,tick)
            if op is None:break
            phase,version=op['phase'],op['phase_version']
            error=None
            try:result=self._external(op)
            except PaymentError as exc:result={'ok':False,'code':exc.code}
            except (OSError,sqlite3.Error):result=None;error='RESPONSE_NOT_CONFIRMED'
            with self.store.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                current=self.store.operations._op(db,oid);s=self.store._load(db,sid)
                if (current['status']!='PENDING' or current['phase_version']!=version
                        or current['phase']!=phase or s.get('active_operation')!=oid):
                    db.execute('COMMIT');continue
                current['attempts']+=1
                if result is None:
                    current['message']='매장·결제 응답을 확인하지 못했어요. 기존 작업을 다시 확인하고 있어요.'
                    current['history'].append(dict(phase=phase,label=TEXT[phase],result=error))
                else:
                    self._observe(s,current,phase,result)
                    current['results'][phase]=deepcopy(result)
                    current['history'].append(dict(phase=phase,label=TEXT[phase],result='CONFIRMED' if result['ok'] else result.get('code','REJECTED')))
                    transition(current,result)
                after_step(current,tick,result is None)
                if current['recovery']['state']=='REVIEW_REQUIRED':
                    current['message']='매장·결제 기록을 더 확인해야 해요. 주문이나 청구를 새로 만들지 않았어요.'
                if current['status']=='COMPLETED':s=self._finalize(s,current)
                elif current['status']=='REJECTED' and s['order'] is None:
                    s['payment']['state']='DECLINED' if current['failure'] in {'DECLINED','LIMIT_EXCEEDED'} else 'VOIDED'
                s['version']=self.store._load(db,sid)['version']+1
                current['phase_version']+=1
                if current['status']!='PENDING':s.pop('active_operation',None);s.pop('pending_order_id',None)
                s['handoff']=public(current)
                self.store.operations._save_op(db,current);self.store._save(db,s)
                db.execute('COMMIT')
            if result is None or current['status']!='PENDING' or current['recovery']['state']=='REVIEW_REQUIRED':break
        with self.store.connection() as db:s=self.store._load(db,sid)
        return self.store.view(s,duplicate)
