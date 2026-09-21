#!/usr/bin/env python3
from __future__ import annotations
import argparse,json,random
from pathlib import Path
from consistency_benchmark import capacity_race,event_consistency

def run_seed(seed:int,orders:int)->dict[str,int]:
    rng=random.Random(seed); baseline_over,pact_over=capacity_race(rng,orders); dup_base,dup_pact,stale_base,stale_pact,repairs,event_count=event_consistency(rng,orders)
    return {"seed":seed,"orders":orders,"simulated_events":event_count,"capacity_oversubscribed_units_baseline":baseline_over,"capacity_oversubscribed_units_pickup_pact":pact_over,"duplicate_financial_posts_baseline":dup_base,"duplicate_financial_posts_pickup_pact":dup_pact,"stale_financial_orders_after_late_cancel_baseline":stale_base,"stale_financial_orders_after_late_cancel_pickup_pact":stale_pact,"repair_commands_emitted":repairs}

def main():
    p=argparse.ArgumentParser();p.add_argument("--orders",type=int,default=20000);p.add_argument("--seeds",default="11,22,33,44,55");p.add_argument("--output",default="artifacts/consistency-matrix.json");a=p.parse_args();seeds=[int(v.strip()) for v in a.seeds.split(",") if v.strip()];runs=[run_seed(s,a.orders) for s in seeds]
    keys=["orders","simulated_events","capacity_oversubscribed_units_baseline","capacity_oversubscribed_units_pickup_pact","duplicate_financial_posts_baseline","duplicate_financial_posts_pickup_pact","stale_financial_orders_after_late_cancel_baseline","stale_financial_orders_after_late_cancel_pickup_pact","repair_commands_emitted"]
    out={"benchmark":"synthetic-consistency-matrix-v1","scope":"Five deterministic synthetic correctness runs; not production traffic or latency.","runs":runs,"totals":{k:sum(r[k] for r in runs) for k in keys}}
    path=Path(a.output);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(out,indent=2,sort_keys=True)+"\n",encoding="utf-8");print(json.dumps(out,indent=2,sort_keys=True))
if __name__=="__main__":main()
