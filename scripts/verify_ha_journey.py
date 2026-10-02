#!/usr/bin/env python3
"""Native three-database HTTP integration and same UI, on one development host."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4
from ha_development_stack import stack, request


def command(base,state,action,**extra):
    return request(base,'/api/route/journeys/'+state['id']+'/commands',
                   dict(action=action,expected_version=state['version'],request_id=uuid4().hex,**extra))


def settle(base,state):
    deadline=time.monotonic()+20
    while state['handoff_pending'] and time.monotonic()<deadline:
        time.sleep(.15);state=request(base,'/api/route/journeys/'+state['id'])
    assert not state['handoff_pending'],'automatic recovery did not finish'
    return state


def scenario(topology,fault):
    a,b=topology.urls
    state=request(a,'/api/route/journeys',{'coupon_id':'welcome500','points':1000,'payment_scenario':fault})
    wave=next(x for x in state['all_plans'] if x['store_id']=='wave')
    state=command(a,state,'reserve',quote_id=wave['quote_id'])
    if fault=='authorize_reply_lost':
        assert state['handoff_pending'] and state['order'] is None
        topology.stop('app-0')
        state=settle(b,state)
        topology.start_app(0)
    oid=state['order']['id']
    oat=next(x for x in state['all_plans'] if x['store_id']=='oat')
    state=command(b,state,'transfer',quote_id=oat['quote_id'])
    state=settle(a,state)
    assert state['order']['id']==oid and state['order']['price']==3200
    minutes=max(0,state['current_plan']['start_at']-state['clock'])
    if minutes:state=command(a,state,'advance',minutes=minutes)
    state=command(b,state,'start')
    state=command(a,state,'advance',minutes=max(0,state['order']['ready_at']-state['clock']))
    state=command(b,state,'ready')
    state=command(a,state,'claim',pickup_code=state['order']['pickup_code'])
    if fault=='capture_reply_lost':
        assert state['handoff_pending']
        topology.stop('app-0')
        topology.stop('worker-0')
        state=settle(b,state)
        topology.start_app(0);topology.start_worker(0)
    else:state=settle(b,state)
    proof=request(b,'/api/route/journeys/'+state['id']+'/reconciliation')
    assert proof['status']=='MATCH' and proof['terminal']
    assert proof['payment']['capture_count']==1 and proof['payment']['captured_krw']==3200 and proof['payment']['held_krw']==0
    assert state['wallet']['spent']==1000 and state['wallet']['earned']==32
    assert proof['merchants']['wave']['reservation']['phase']=='RELEASED'
    assert proof['merchants']['oat']['reservation']['phase']=='CLAIMED'
    return dict(fault=fault,order_id=oid,proof=proof,passed=True)


def verify(output,repeat,browsers):
    output.mkdir(parents=True,exist_ok=False)
    report={'scope':'single_host_native_postgresql_development','results':[],'passed':False}
    try:
        with stack(output/'processes') as topology:
            report['runtime']=request(topology.urls[0],'/api/route/runtime')
            for number in range(repeat):
                for fault in ['none','authorize_reply_lost','capture_reply_lost','void_reply_lost','notification_duplicate','notification_late']:
                    row=scenario(topology,fault);row['repeat']=number+1;report['results'].append(row)
            if browsers:
                for name,script in [('guided','verify_guided_browser.py'),('handoff','verify_handoff_browser.py'),
                        ('route','verify_route_browser.py'),('benefits','verify_benefits_browser.py'),
                        ('decision','verify_decision_browser.py'),('copy','verify_demo_copy_browser.py'),
                        ('payment','verify_payment_browser.py'),('tabs','verify_payment_tabs.py')]:
                    with (output/(name+'.log')).open('w') as log:
                        subprocess.run([sys.executable,'scripts/'+script,'--base-url',topology.urls[0],
                            '--repeat',str(repeat),'--expected-commit',os.environ['RENDER_GIT_COMMIT'],
                            '--output',str(output/'browser'/name)],stdout=log,stderr=subprocess.STDOUT,check=True,timeout=900)
            report['passed']=True
    except Exception as exc:
        report['error']=type(exc).__name__+': '+str(exc)
        raise
    finally:(output/'results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({'passed':report['passed'],'scenarios':len(report['results']),'scope':report['scope']}))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--repeat',type=int,default=3,choices=range(1,4))
    parser.add_argument('--browsers',action='store_true')
    args=parser.parse_args();verify(args.output,args.repeat,args.browsers)
