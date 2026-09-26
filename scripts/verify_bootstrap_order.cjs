'use strict';
// Exercise the real startup code with controlled deferred-listener timing.
// This is a unit check, not a substitute for real HTTP Chromium verification.
const fs=require('node:fs'), vm=require('node:vm'), assert=require('node:assert/strict');
const source=fs.readFileSync('demo/route/product.js','utf8');
const marker=source.indexOf('// Route boot:');
const legacy=source.lastIndexOf('(async()=>{try{catalog=');
assert(marker>=0||legacy>=0,'startup code not found');
const startup=source.slice(marker>=0?marker:legacy);
const flush=async()=>{for(let n=0;n<12;n++)await Promise.resolve();};
function harness(ready='interactive',saved='saved-order',bad=false){
 const events={},requests=[],warnings=[];let renders=0,received=0,removals=0;
 const ctx=vm.createContext({catalog:null,state:null,
  document:{readyState:ready,addEventListener:(type,fn,options)=>{
    (events[type]??=[]).push({fn,once:options?.once});}},
  api:async path=>{requests.push(path);if(bad)throw Error('unavailable');
    return path.endsWith('/catalog')?{stores:[]}:{id:saved,order:{state:'CANCELLED'},wallet:{available_points:2000}};},
  localStorage:{getItem:()=>saved,removeItem:()=>removals++},
  drawMap:()=>{},fillIntent:()=>{},renderAux:()=>{},
  render:()=>{renders++;for(const entry of events['route:render']||[])entry.fn({detail:ctx.state});},
  notify:(text,error)=>warnings.push({text,error})});
 vm.runInContext(startup,ctx);
 return {requests,warnings,get renders(){return renders;},get received(){return received;},
  get removals(){return removals;},install:()=>{(events['route:render']??=[]).push({fn:e=>{assert.equal(e.detail.wallet.available_points,2000);received++;}});},
  ready:()=>{for(const event of [...(events.DOMContentLoaded||[])])event.fn();events.DOMContentLoaded=(events.DOMContentLoaded||[]).filter(e=>!e.once);}};
}
const cases={
 'deferred scripts may still be loading while readyState is interactive':async()=>{const h=harness();await flush();assert.equal(h.requests.length,0,'initial API/render started before deferred extensions registered');},
 'late benefit listener receives restored cancelled-order state exactly once':async()=>{const h=harness();await flush();h.install();h.ready();await flush();assert.equal(h.received,1);assert.equal(h.renders,1);},
 'DOMContentLoaded initializes once, not once per notification':async()=>{const h=harness();h.install();h.ready();h.ready();await flush();assert.equal(h.requests.length,2);assert.equal(h.renders,1);},
 'a fully loaded document can initialize immediately':async()=>{const h=harness('complete');await flush();assert.equal(h.requests.length,2);assert.equal(h.renders,1);},
 'a fresh visit does not invent or fetch an order':async()=>{const h=harness('interactive',null);h.ready();await flush();assert.deepEqual(h.requests,['/api/route/catalog']);assert.equal(h.renders,0);},
 'startup network failure remains an error, not a restored order':async()=>{const h=harness('interactive','saved-order',true);h.ready();await flush();assert.equal(h.renders,0);assert.equal(h.warnings.length,1);assert.equal(h.warnings[0].error,true);}
};
(async()=>{let failed=0;for(const [name,test] of Object.entries(cases)){try{await test();console.log('PASS',name);}catch(error){failed++;console.error('FAIL',name,error.message);}}
 console.log(`${Object.keys(cases).length-failed}/${Object.keys(cases).length} bootstrap-order checks passed`);process.exitCode=failed?1:0;
})().catch(e=>{console.error(e);process.exit(1);});
