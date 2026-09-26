#!/usr/bin/env python3
"""Paired local setup benchmark; identical command handlers and durability.

This is not production latency or a new independent sample of customer outcomes.
"""
import argparse,json,platform,tempfile,time
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from demo.route.api import Intent
from demo.route.planner import digest
from demo.route.transfer_comparison import _one,SCENARIOS,POLICIES,run_transfer_comparison


def reference(intent):
    rows=[]
    with tempfile.TemporaryDirectory(prefix='comparison-reference-') as tmp:
        for scenario,label in SCENARIOS.items():
            row=dict(scenario=scenario,label=label)
            for policy in POLICIES:
                row[policy]=_one(Path(tmp)/scenario/policy,intent,scenario,policy)[0]
            rows.append(row)
    return dict(intent=intent,policies=POLICIES,cases=rows)


def main(output):
    intent=Intent(coupon_id='welcome500',points=1000).model_dump();results=[];expected=None
    for i in range(3):
        for mode in (('reference','shared') if i%2==0 else ('shared','reference')):
            began=time.perf_counter()
            value=reference(intent) if mode=='reference' else run_transfer_comparison(intent)
            elapsed=time.perf_counter()-began
            semantic={k:value[k] for k in ('intent','policies','cases')}
            fingerprint=digest(semantic)
            if expected is not None: assert fingerprint==expected
            expected=fingerprint
            results.append(dict(pass_number=i+1,mode=mode,seconds=elapsed,semantic_sha256=fingerprint))
    report=dict(platform=platform.platform(),python=platform.python_version(),rows=results,
        setup_count=dict(reference=24,shared=1),policies=4,scenarios=6,
        durability='Unchanged: independent merchant SQLite commits, WAL/FULL',
        limits='Local paired execution only. Includes real disk and commands. No claimed public latency, customer improvement, or competitor measurement.')
    output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,default=Path('verification/comparison-setup-benchmark.json'));main(p.parse_args().output)
