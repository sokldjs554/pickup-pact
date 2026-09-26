#!/usr/bin/env python3
"""Actual HTTP acceptance checks; does not stand in for browser UI checks."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request,urlopen
from uuid import uuid4


def verify(base:str, output:Path, repeat:int=3, expected_commit:str|None=None)->dict:
    output.mkdir(parents=True,exist_ok=True)
    def api(path,body=None,expected=200):
        data=None if body is None else json.dumps(body).encode()
        request=Request(base+path,data=data,headers={'Content-Type':'application/json'})
        try:
            with urlopen(request,timeout=45) as r: status=r.status;result=json.load(r)
        except HTTPError as e: status=e.code;result=json.load(e)
        assert status==expected,(path,status,expected,result)
        return result
    health=api('/health')
    if expected_commit:assert health['release_commit']==expected_commit,health
    automatic=api('/api/route/runtime')['automatic_recovery']
    runs=[]
    for repetition in range(1,repeat+1):
        s=api('/api/route/journeys',{'coupon_id':'welcome500','points':1000},201)
        sid=s['id'];url=f'/api/route/journeys/{sid}'
        def command(action,**extra):
            nonlocal s
            s=api(url+'/commands',dict(action=action,expected_version=s['version'],request_id=uuid4().hex,**extra))
            return s
        def control(action,**extra):
            nonlocal s
            s=api(url+'/transfer-controls',dict(action=action,expected_version=s['version'],request_id=uuid4().hex,**extra));return s
        def quote(shop):return next(p for p in s['all_plans'] if p['store_id']==shop)['quote_id']
        command('reserve',quote_id=quote('wave'));command('disrupt',store_id='wave',minutes=12)
        original=json.loads(json.dumps(s));oid=s['order']['id']
        assert s['order']['price']==2800 and s['wallet']['held_points']==1000
        control('fault',fault='target_reject')
        command('transfer',quote_id=quote('oat'))
        assert s['handoff']['status']=='REJECTED'
        assert s['order']==original['order'] and s['wallet']==original['wallet']
        if repetition==1:(output/'rejected.json').write_text(json.dumps(s,ensure_ascii=False,indent=2))
        control('fault',fault='after_target_hold')
        command('transfer',quote_id=quote('oat'))
        assert s['handoff_pending'] and s['handoff']['status']=='PENDING'
        assert s['merchant_capacity']['oat']['used']==s['merchant_capacity']['wave']['used']==1
        assert s['order']==original['order'] and s['wallet']==original['wallet']
        pending=api(url)
        assert pending==s
        # Requests with other actions may not spend or manufacture while ownership is uncertain.
        rejected=api(url+'/commands',dict(action='cancel',expected_version=s['version'],request_id=uuid4().hex),409)
        assert rejected['detail']['code']=='TRANSFER_PENDING'
        if repetition==1:(output/'pending.json').write_text(json.dumps(s,ensure_ascii=False,indent=2))
        if automatic:
            deadline=time.monotonic()+15
            while s['handoff_pending'] and time.monotonic()<deadline:
                time.sleep(.1);s=api(url)
            assert not s['handoff_pending'],s
        else:
            command('recover')
        assert s['handoff']['status']=='COMPLETED' and s['order']['id']==oid
        assert s['order']['store_id']=='oat' and s['order']['price']==3200
        assert s['receipt']['authorization_count']==1 and len(s['receipt']['transfers'])==1
        assert s['merchant_capacity']['wave']['used']==0 and s['merchant_capacity']['oat']['used']==1
        if repetition==1:(output/'recovered.json').write_text(json.dumps(s,ensure_ascii=False,indent=2))
        command('advance',minutes=max(0,s['current_plan']['start_at']-s['clock']))
        command('start');command('advance',minutes=s['order']['ready_at']-s['clock']);command('ready')
        request=dict(action='claim',pickup_code=s['order']['pickup_code'],expected_version=s['version'],request_id=uuid4().hex)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda _:api(url+'/commands',request),range(24)))
        s=api(url)
        assert all(r['receipt']['capture_count']==1 for r in results)
        assert s['receipt']['net_paid']==3200 and s['wallet']['spent']==1000 and s['wallet']['earned']==32
        assert s['wallet']['balance']==1032 and s['merchant_capacity']['oat']['used']==0
        if repetition==1:(output/'receipt.json').write_text(json.dumps(s,ensure_ascii=False,indent=2))
        comparison=api('/api/route/transfer-comparison',{'intent':{'coupon_id':'welcome500','points':1000}})
        normal=next(c for c in comparison['cases'] if c['scenario']=='normal')
        assert normal['cancel_reorder']['cash_due']==normal['guarded_transfer']['cash_due']==3200
        assert normal['cancel_reorder']['predicted_arrival']==normal['guarded_transfer']['predicted_arrival']==9
        declined=next(c for c in comparison['cases'] if c['scenario']=='reject_after_quote')
        assert not declined['cancel_reorder']['has_order'] and declined['guarded_transfer']['original_preserved']
        (output/f'comparison-{repetition}.json').write_text(json.dumps(comparison,ensure_ascii=False,indent=2))
        runs.append(dict(repetition=repetition,result='passed',base_url=base,release_commit=health.get('release_commit'),
            order_id=oid,checks=['actual_http','target_rejection_retains_order_wallet','uncertain_reply_not_success',
             'read_reload_retains_pending','competing_action_rejected','recover_same_id','merchant_prepare',
             '24_concurrent_pickup_retries','one_capture','points_spent_earned_once','four_policy_comparison'],
             comparison_semantic_sha256=comparison['semantic_sha256']))
    report=dict(mode='actual_http_not_browser',runs=runs,all_passed=True)
    (output/'http-results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    return report

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--base-url',default='http://127.0.0.1:10000')
    p.add_argument('--output',type=Path,default=Path('verification/handoff-http'));p.add_argument('--repeat',type=int,default=3)
    p.add_argument('--expected-commit');a=p.parse_args()
    if not 1<=a.repeat<=10:raise SystemExit('repeat must be between 1 and 10')
    print(json.dumps(verify(a.base_url.rstrip('/'),a.output,a.repeat,a.expected_commit),ensure_ascii=False,indent=2))
