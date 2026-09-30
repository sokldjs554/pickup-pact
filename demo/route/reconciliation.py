"""Read independent order/merchant/payment sources without mutating any of them.

Two bounded observations detect changing revisions. This is an observed stable
window, not a distributed snapshot or a claim that future changes cannot happen.
"""
from __future__ import annotations
from copy import deepcopy
import sqlite3
import time
from . import benefits

LABELS={'order':'주문 번호 유지','merchant':'수령 매장 확인','payment':'청구·승인 보류 확인','benefits':'쿠폰·포인트 확인'}


def reconcile(store,sid: str) -> dict:
    began=time.time()
    def coordinator():
        with store.connection() as db:return store._load(db,sid)
    s0=coordinator();order=s0.get('order')
    oid=(order or {}).get('id') or s0.get('pending_order_id') or s0.get('payment_order_id')
    result=dict(mode='synthetic',scope='single_host_independent_process_and_store_databases',
                journey_id=sid,order_id=oid,journey_version=s0['version'],status='PENDING',terminal=False,
                payment=None,merchants=None,checks={k:dict(status='PENDING',label=v,detail='주문 처리를 확인하고 있어요.') for k,v in LABELS.items()},
                observation=dict(started_at=began,finished_at=None,stable=False,coordinator_version=s0['version']))
    def finish(status):
        result['status']=status;result['observation']['finished_at']=time.time();return result
    if s0.get('protocol_version')!=2:
        result['scope']='legacy_local_simulation'
        for item in result['checks'].values():item.update(status='UNAVAILABLE',detail='이전 체험은 외부 결제 대조 대상이 아니에요.')
        return finish('UNAVAILABLE')
    if not oid:return finish('PENDING')
    unavailable=set()
    def observe():
        m=p=None
        try:m=store.fleet.evidence(s0['world_id'],oid)
        except (OSError,sqlite3.Error,ValueError):unavailable.add('merchant')
        try:
            if store.payment_gateway is None:raise OSError('payment unavailable')
            p=store.payment_gateway.snapshot(s0['world_id'],oid)
        except (OSError,sqlite3.Error,ValueError):unavailable.add('payment')
        return m,p
    m0,p0=observe();m1,p1=observe();s1=coordinator()
    result['payment']=None if 'payment' in unavailable else p1
    result['merchants']=None if 'merchant' in unavailable else m1['merchants']
    if unavailable:
        for k in unavailable:result['checks'][k].update(status='UNAVAILABLE',detail='원본 서버의 자료를 읽지 못했어요. 0건으로 판단하지 않아요.')
        return finish('UNAVAILABLE')
    stable=(s0==s1 and m0==m1 and p0==p1)
    result['observation'].update(stable=stable,merchant_revisions={k:v['revision'] for k,v in m1['merchants'].items()},payment_revision=p1['revision'])
    if not stable:
        for item in result['checks'].values():item['detail']='조회하는 동안 상태가 바뀌었어요. 다음 조회로 확인해요.'
        return finish('PENDING')
    if s1.get('active_operation'):
        for item in result['checks'].values():item['detail']='저장된 작업이 아직 진행 중이에요. 일부 서버의 성공만으로 완료하지 않아요.'
        return finish('PENDING')
    if not order:
        clean=p1['captured_krw']==0 and p1['held_krw']==0
        for item in result['checks'].values():item.update(status='MATCH' if clean else 'MISMATCH',detail='확정 주문 없이 종료됐어요. 승인 보류와 청구 내역을 확인하세요.')
        result['terminal']=True
        return finish('MATCH' if clean else 'MISMATCH')
    state=order['state'];final=state in {'PICKED_UP','CANCELLED'};price=order['price']
    result['terminal']=final
    entries=m1['merchants'];rows=[(shop,v['reservation']) for shop,v in entries.items() if v['reservation']]
    claimed=[shop for shop,row in rows if row['phase']=='CLAIMED']
    active=[shop for shop,row in rows if row['phase'] in {'RESERVED','HELD','FROZEN','PREPARING','READY'}]
    expected_phase={'RESERVED':'RESERVED','PREPARING':'PREPARING','READY':'READY','PICKED_UP':'CLAIMED','CANCELLED':'CANCELLED'}[state]
    target=entries[order['store_id']]['reservation']
    merchant_ok=bool(target and target['phase']==expected_phase and target['generation']==order['merchant_generation'])
    merchant_ok &= ((claimed==[order['store_id']] and not active) if state=='PICKED_UP' else
                    (not claimed and not active) if state=='CANCELLED' else (active==[order['store_id']] and not claimed))
    expected_cash=price if state=='PICKED_UP' else 0
    expected_hold=price if not final else 0
    payment_ok=(p1['captured_krw']==expected_cash and p1['capture_count']==int(state=='PICKED_UP' and price>0)
                and p1['held_krw']==expected_hold)
    auth=s1.get('payment',{}).get('authorization')
    if price and state!='CANCELLED':
        matching=[a for a in p1['authorizations'] if auth and a['authorization_id']==auth['authorization_id']]
        payment_ok &= bool(len(matching)==1 and matching[0]['amount_krw']==price and matching[0]['payment_revision']==auth['payment_revision']
                           and matching[0]['status']==('CAPTURED' if state=='PICKED_UP' else 'AUTHORIZED'))
    wallet=benefits.wallet_view(s1);pricing=order['pricing'];used=pricing['points_used'];earned=pricing['points_to_earn']
    consumed=[e for e in s1['events'] if e['type']=='BENEFITS_CONSUMED']
    benefits_ok=(wallet['spent']==(used if state=='PICKED_UP' else 0) and wallet['earned']==(earned if state=='PICKED_UP' else 0)
                 and wallet['held_points']==(used if not final else 0) and len(consumed)==int(state=='PICKED_UP'))
    if state=='PICKED_UP':benefits_ok &= p1['captured_krw']==price
    order_ok=s1.get('first_order_id')==oid
    outcomes={'order':(order_ok, '같은 주문 번호 '+oid if order_ok else '처음 주문과 번호가 달라요.'),
              'merchant':(merchant_ok, '수령 매장 1곳 · '+order['store_name'] if state=='PICKED_UP' else '현재 매장 상태: '+expected_phase),
              'payment':(payment_ok,f"확정 청구 {p1['captured_krw']:,}원 · {p1['capture_count']}건 / 남은 승인 보류 {p1['held_krw']:,}원"),
              'benefits':(benefits_ok,f"{wallet['spent']:,}P 사용 · {wallet['earned']:,}P 적립 / 보류 {wallet['held_points']:,}P")}
    for k,(ok,detail) in outcomes.items():result['checks'][k].update(status='MATCH' if ok else 'MISMATCH',detail=detail)
    result['order']=dict(id=oid,store_id=order['store_id'],store_name=order['store_name'],state=state,amount_krw=price)
    result['benefits']=dict(spent=wallet['spent'],earned=wallet['earned'],held_points=wallet['held_points'])
    return finish('MATCH' if all(ok for ok,_ in outcomes.values()) else 'MISMATCH')
