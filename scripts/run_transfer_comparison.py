#!/usr/bin/env python3
"""Export controlled stay/cancel-reorder/guarded-transfer command executions."""
from __future__ import annotations
import argparse
import csv
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from demo.route.api import Intent
from demo.route.transfer_comparison import run_transfer_comparison


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=Path('verification/transfer-comparison'))
    parser.add_argument('--repeat',type=int,default=3)
    args=parser.parse_args()
    if not 1<=args.repeat<=10:raise SystemExit('repeat must be between 1 and 10')
    args.output.mkdir(parents=True,exist_ok=True)
    intent=Intent(coupon_id='welcome500',points=1000).model_dump()
    previous=None
    for i in range(1,args.repeat+1):
        report=run_transfer_comparison(intent)
        if previous is not None:assert report['semantic_sha256']==previous
        previous=report['semantic_sha256']
        (args.output/f'execution-{i}.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    with (args.output/'comparison.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=['scenario','policy',*report['cases'][0]['stay'].keys()]);writer.writeheader()
        for row in report['cases']:
            for policy in report['policies']:
                writer.writerow({'scenario':row['scenario'],'policy':policy,**row[policy]})
    result=dict(schedules=6,policies=4,repeat=args.repeat,semantic_sha256=previous,
        note='Repeated executions are not additional independent samples. IDs differ; modeled results are identical. No real-user click latency or market superiority is measured.')
    (args.output/'reproducibility.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
