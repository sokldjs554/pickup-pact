#!/usr/bin/env python3
from __future__ import annotations
import argparse,asyncio,json,statistics
from pathlib import Path
from load_http import run
async def main(url,requests,concurrencies,repeats):
    await run(url,min(100,requests),min(10,max(concurrencies)));groups=[]
    for concurrency in concurrencies:
        runs=[await run(url,requests,concurrency) for _ in range(repeats)]
        groups.append({"concurrency":concurrency,"runs":runs,"median_of_runs":{"requests_per_s":round(statistics.median(r["requests_per_s"] for r in runs),2),"p95_ms":round(statistics.median(r["latency_ms"]["p95"] for r in runs),2),"p99_ms":round(statistics.median(r["latency_ms"]["p99"] for r in runs),2),"failures":int(statistics.median(r["failures"] for r in runs))}})
    return {"benchmark":"reconciler-loopback-http-matrix-v1","scope":"Repeated loopback FastAPI reconciliation runs; persistence disabled; excludes PostgreSQL/Redis/Kafka/MongoDB/Elasticsearch; not production traffic.","requests_per_run":requests,"repeats":repeats,"groups":groups}
if __name__=="__main__":
    p=argparse.ArgumentParser();p.add_argument("--url",default="http://localhost:8000/api/v1/reconcile");p.add_argument("--requests",type=int,default=2000);p.add_argument("--concurrencies",default="10,50,100");p.add_argument("--repeats",type=int,default=3);p.add_argument("--output",default="artifacts/reconciler-http-matrix.json");a=p.parse_args();cs=[int(x.strip()) for x in a.concurrencies.split(",") if x.strip()];result=asyncio.run(main(a.url,a.requests,cs,a.repeats));rendered=json.dumps(result,indent=2,sort_keys=True);print(rendered);path=Path(a.output);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(rendered+"\n",encoding="utf-8")
