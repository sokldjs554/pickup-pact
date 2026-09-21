#!/usr/bin/env python3
from __future__ import annotations
import argparse,asyncio,json,os,platform,statistics,time
from pathlib import Path
import httpx
PAYLOAD={"events":[{"event_id":"hold","aggregate_id":"load-order","event_type":"PickupSlotHeld","occurred_at":"2026-09-18T03:00:00Z","received_at":"2026-09-18T03:00:00Z","payload":{"capacity_units":1}},{"event_id":"pay","aggregate_id":"load-order","event_type":"PaymentAuthorized","occurred_at":"2026-09-18T03:00:01Z","received_at":"2026-09-18T03:00:01Z","payload":{}},{"event_id":"confirm","aggregate_id":"load-order","event_type":"CommitmentConfirmed","occurred_at":"2026-09-18T03:00:02Z","received_at":"2026-09-18T03:00:02Z","payload":{}}]}
def percentile(ordered,q):
    if not ordered:return 0.0
    return ordered[min(len(ordered)-1,max(0,int(len(ordered)*q)-1))]
async def run(url,requests,concurrency):
    semaphore=asyncio.Semaphore(concurrency);latencies=[];failures=0;started_all=time.perf_counter()
    async with httpx.AsyncClient(timeout=10) as client:
        async def one(i):
            nonlocal failures
            payload={"events":[dict(e,aggregate_id=f"load-order-{i}") for e in PAYLOAD["events"]]}
            async with semaphore:
                started=time.perf_counter();response=await client.post(url,json=payload);latencies.append((time.perf_counter()-started)*1000);failures+=int(response.status_code!=200)
        await asyncio.gather(*(one(i) for i in range(requests)))
    elapsed=time.perf_counter()-started_all;ordered=sorted(latencies)
    return {"benchmark":"reconciler-loopback-http-v1","scope":"FastAPI /api/v1/reconcile only; loopback HTTP; persistence disabled; no PostgreSQL/Redis/Kafka/MongoDB/Elasticsearch; not production traffic.","requests":requests,"concurrency":concurrency,"failures":failures,"elapsed_s":round(elapsed,3),"requests_per_s":round(requests/elapsed,2),"latency_ms":{"median":round(statistics.median(ordered),2),"p95":round(percentile(ordered,.95),2),"p99":round(percentile(ordered,.99),2),"max":round(max(ordered),2)},"environment":{"python":platform.python_version(),"platform":platform.platform(),"logical_cpus":os.cpu_count()}}
if __name__=="__main__":
    p=argparse.ArgumentParser();p.add_argument("--url",default="http://localhost:8000/api/v1/reconcile");p.add_argument("--requests",type=int,default=1000);p.add_argument("--concurrency",type=int,default=50);p.add_argument("--output");a=p.parse_args();result=asyncio.run(run(a.url,a.requests,a.concurrency));rendered=json.dumps(result,indent=2,sort_keys=True);print(rendered)
    if a.output:
        path=Path(a.output);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(rendered+"\n",encoding="utf-8")
