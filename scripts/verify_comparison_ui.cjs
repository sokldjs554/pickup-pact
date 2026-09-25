'use strict';
// Async UI unit tests with deferred responses. These are not browser E2E.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
function setup(){
 const nodes={},timers=[],calls=[],notices=[];let resolve,reject,runs=0,globalBusy=false;
 const pending=new Promise((a,b)=>{resolve=a;reject=b;});
 const element=id=>nodes[id]??=( {id,disabled:false,hidden:false,innerHTML:'',textContent:'',attrs:{},handlers:{},
  setAttribute(k,v){this.attrs[k]=v;},getAttribute(k){return this.attrs[k];},
  addEventListener(k,fn){this.handlers[k]=fn;},insertAdjacentElement(){},append(){},insertAdjacentHTML(){}} );
 const intent={destination:'office',deadline_minutes:16,drink:'latte',milk:'regular',decaf:false,budget:5500,max_detour:5,priority:'arrival',coupon_id:'welcome500',points:1000};
 const ctx=vm.createContext({console,JSON,Number,Set,Object,String,Date,
  state:{id:'first',intent},catalog:{stores:[]},drink:'latte',
  document:{querySelector:q=>q==='.main-column'?element('main'):null,querySelectorAll:()=>[],createElement:()=>element('created-'+Object.keys(nodes).length),addEventListener:()=>{}},
  $:element,esc:s=>String(s??''),won:n=>`${n}원`,time:n=>`${n}분`,labelIntent:i=>i.drink,
  notify:(...args)=>notices.push(args),run:async fn=>{if(globalBusy)return;globalBusy=true;runs++;try{return await fn();}catch(e){notices.push([e.message,true]);}finally{globalBusy=false;}},
  api:async(...args)=>{calls.push(args);return pending;},
  fetch:async(url,options)=>{calls.push([url,JSON.parse(options.body),{signal:options.signal}]);const payload=await pending;return {ok:true,status:200,json:async()=>payload};},
  friendlyRouteError:detail=>detail,
  setTimeout:fn=>(timers.push(fn),timers.length),clearTimeout:()=>{},
  AbortController:class{constructor(){this.signal={aborted:false};}abort(){this.signal.aborted=true;}},
  URL:{createObjectURL:()=>'',revokeObjectURL:()=>{}},Blob:class{},
 });
 vm.runInContext(fs.readFileSync('demo/route/handoff-ui.js','utf8'),ctx);
 return {ctx,nodes,timers,calls,notices,resolve,reject,runCount:()=>runs,click:()=>element('compareHandoff').handlers.click()};
}
function response(){const row={has_order:true,original_preserved:true,same_order:true,cash_due:3200,customer_commands:2,same_request_retries:0,predicted_arrival:9};return {intent:{drink:'latte'},disclosure:'실제 이용 통계가 아닌 비교',cases:Array.from({length:6},(_,i)=>({label:'상황'+i,cancel_reorder:row,guarded_transfer:row,stay:row}))};}
const tests=[
 ['pending feedback and independent order controls',async()=>{const t=setup();const done=t.click();assert.equal(t.calls.length,1);assert.equal(t.nodes.compareHandoff.disabled,true);assert.equal(t.nodes.handoffComparisonResult.attrs['aria-busy'],'true');assert.match(t.nodes.handoffComparisonResult.innerHTML,/비교하고/);assert.equal(t.runCount(),0,'a read-only comparison must not acquire the global order command lock');t.resolve(response());await done;assert.equal(t.nodes.compareHandoff.disabled,false);assert.equal(t.nodes.handoffComparisonResult.attrs['aria-busy'],'false');assert.equal((t.nodes.handoffComparisonResult.innerHTML.match(/<tr>/g)||[]).length,7);} ],
 ['duplicate click sends only one request',async()=>{const t=setup();const a=t.click(),b=t.click();assert.equal(t.calls.length,1);t.resolve(response());await Promise.all([a,b]);}],
 ['failed request is visible and never success',async()=>{const t=setup();const done=t.click();t.reject(Object.assign(Error('다른 비교가 실행 중이에요.'),{status:429}));await done;assert.equal(t.nodes.compareHandoff.disabled,false);assert.match(t.nodes.handoffComparisonResult.innerHTML,/다른 비교/);assert(!t.nodes.handoffComparisonResult.innerHTML.includes('<table>'));assert(!t.notices.some(([text,error])=>!error&&text.includes('실행했어요')));} ],
 ['comparison inputs are frozen while order changes',async()=>{const t=setup();const done=t.click();t.ctx.state.intent.budget=1000;assert.equal(t.calls[0][1].intent.budget,5500);t.resolve(response());await done;assert.match(t.nodes.handoffComparisonResult.innerHTML,/5500원/);} ],
 ['lost response timeout does not change orders',async()=>{const t=setup();const original=JSON.stringify(t.ctx.state);const done=t.click();assert.equal(t.timers.length,1);t.timers[0]();assert(t.calls[0][2].signal.aborted);t.reject(Object.assign(Error('aborted'),{name:'AbortError'}));await done;assert.equal(JSON.stringify(t.ctx.state),original);assert.match(t.nodes.handoffComparisonResult.innerHTML,/다시/);assert.equal(t.nodes.compareHandoff.disabled,false);} ],
];
(async()=>{let failures=0;for(const[name,test]of tests){try{await test();console.log('PASS',name);}catch(e){failures++;console.error('FAIL',name,e.message);}}if(failures)process.exitCode=1;else console.log(`${tests.length} comparison async UI checks passed`);})();
