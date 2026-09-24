#!/usr/bin/env python3
"""Reproduce paired cases; output includes all losses and unserviceable inputs."""
import argparse
import csv
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from demo.route.outcomes import run_experiment

if __name__ == '__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--seeds',type=int,nargs='+',default=[7,19,31])
    p.add_argument('--cases',type=int,default=120)
    p.add_argument('--output',type=Path,default=Path('verification/route-outcomes'))
    args=p.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    for seed in args.seeds:
        report=run_experiment(seed,args.cases)
        (args.output/f'seed-{seed}.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        with (args.output/f'seed-{seed}.csv').open('w',newline='',encoding='utf-8-sig') as f:
            writer=csv.writer(f);writer.writerow(['case','scenario','initially_serviceable','transferred','reason','stay_arrival','transfer_arrival','deadline','stay_cash','transfer_cash','stay_on_time','transfer_on_time'])
            for r in report['cases']:
                a,b=r['stay'],r['transfer']
                writer.writerow([r['case_id'],r['scenario'],a['initially_serviceable'],r['transferred'],r['decision_reason'],a['arrival_at'],b['arrival_at'],r['intent']['deadline_minutes'],a['cash_due'],b['cash_due'],a['on_time'],b['on_time']])
        print(seed,report['semantic_sha256'],json.dumps({k:v for k,v in report['summary'].items() if k!='by_scenario'},ensure_ascii=False))
