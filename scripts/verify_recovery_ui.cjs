'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
async function test(kind){
  const events={},timers=[],nodes={handoffMessage:{textContent:''},recoverHandoff:{textContent:''}};let rendered=0,calls=0;
  const current={id:'a',version:10,handoff_pending:true,automatic_recovery_enabled:true,handoff:{recovery:{state:'SCHEDULED'}}};
  const ctx=vm.createContext({state:current,setTimeout:fn=>(timers.push(fn),timers.length),clearTimeout:()=>{},
    document:{hidden:false,getElementById:id=>nodes[id]||null,addEventListener:(e,fn)=>events[e]=fn},
    render:()=>rendered++,api:async(path)=>{assert(path.startsWith('/api/route/journeys/'));calls++;
      if(kind==='session'){ctx.state={...current,id:'new'};return {...current,version:11};}
      if(kind==='error')throw Error('offline');
      return {...current,version:kind==='stale'?9:11,handoff_pending:false};}});
  vm.runInContext(fs.readFileSync('demo/route/recovery-ui.js','utf8'),ctx);
  events['route:render']({detail:current});assert(nodes.handoffMessage.textContent.includes('자동으로'));assert.equal(nodes.recoverHandoff.textContent,'지금 다시 확인하기');
  await timers.shift()();
  assert.equal(calls,1);assert.equal(rendered,kind==='fresh'?1:0);
  assert.equal(ctx.state.id,kind==='session'?'new':'a');
  assert.equal(ctx.state.version,kind==='fresh'?11:10);
}
(async()=>{for(const kind of ['fresh','stale','session','error'])await test(kind);console.log('4 recovery UI checks passed: GET-only, stale response, session switch, lost response');})().catch(e=>{console.error(e);process.exit(1);});
