'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
class Element {
  constructor(){this.dataset={};this.handlers={};this.children=[];this.value='none';}
  addEventListener(name,fn){this.handlers[name]=fn;}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=nodes;}
}
const order=version=>({id:'journey-1',version,protocol_version:2,order:{id:'ORDER-1',store_id:'wave',state:'RESERVED'},events:[],stores:[],handoff_pending:false});
const evidence=(version,id='journey-1')=>({journey_id:id,journey_version:version,status:'MATCH',terminal:false,
  observation:{finished_at:1,payment_revision:version,stable:true},payment:{captured_krw:0,held_krw:2800,capture_count:0,transactions:[]},merchants:{},checks:{}});
const settle=async()=>{for(let i=0;i<20;i++)await new Promise(r=>setImmediate(r));};
async function harness(){
  const nodes=new Map(),listeners={},requests=[];let backend={state:order(1),proof:evidence(1)};
  const node=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
  const sandbox={state:order(1),window:{},location:{search:''},URLSearchParams,AbortController,Date,Set,Blob,
    setTimeout:()=>1,clearTimeout:()=>{},document:{hidden:false,getElementById:node,createElement:()=>new Element(),
      addEventListener:(name,fn)=>{listeners[name]=fn;}},
    fetch:async(url,options)=>{requests.push({url,method:options?.method||'GET'});return {ok:true,json:async()=>JSON.parse(JSON.stringify(url.endsWith('/reconciliation')?backend.proof:backend.state))};},
    render:()=>listeners['route:render']({detail:sandbox.state})};
  vm.runInNewContext(fs.readFileSync('demo/route/payment-ui.js','utf8'),sandbox);
  listeners['route:render']({detail:sandbox.state});await settle();assert.equal(node('paymentEvidence').dataset.status,'MATCH');
  return {sandbox,node,requests,change:value=>{backend=value;},refresh:async()=>{await node('refreshPaymentEvidence').handlers.click();await settle();}};
}
(async()=>{
  const failures=[];
  for(const [name,test] of [
    ['another tab changed the order: refresh loads newer coordinator state',async()=>{
      const h=await harness();h.change({state:order(2),proof:evidence(2)});await h.refresh();
      assert.equal(h.sandbox.state.version,2,'refresh must update the stale tab, not retain its old green result');
      assert.equal(h.node('paymentEvidence').dataset.status,'MATCH');
      assert(h.requests.some(r=>r.url.endsWith('/journey-1')));assert(h.requests.every(r=>r.method==='GET'));
    }],
    ['wrong journey proof clears the previous green result',async()=>{
      const h=await harness();h.change({state:order(1),proof:evidence(1,'other-journey')});await h.refresh();
      assert.equal(h.node('paymentEvidence').dataset.status,'UNAVAILABLE');assert.equal(h.sandbox.state.id,'journey-1');
    }],
    ['older revision cannot leave stale success after a requested refresh',async()=>{
      const h=await harness();h.change({state:order(1),proof:evidence(0)});await h.refresh();
      assert.equal(h.node('paymentEvidence').dataset.status,'UNAVAILABLE');assert.equal(h.sandbox.state.version,1);
    }]
  ]){try{await test();console.log('PASS',name);}catch(error){failures.push(name);console.error('FAIL',name,error.message);}}
  assert.equal(failures.length,0,failures.join('\n'));
  console.log('3 actual async refresh checks passed; only GET requests issued');
})().catch(error=>{console.error(error);process.exitCode=1;});
