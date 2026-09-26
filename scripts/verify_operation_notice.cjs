'use strict';
// Isolated UI state checks, not a replacement for real browser verification.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('demo/route/product.js','utf8');
const from=source.indexOf('let state='),to=source.indexOf('function labelIntent',from);
assert(from>=0&&to>from);
function harness(){
 const node={textContent:'',className:'',hidden:true};let scheduled=0;
 const ctx=vm.createContext({$:id=>{assert.equal(id,'notice');return node;},setTimeout:()=>++scheduled,clearTimeout:()=>{},
  document:{body:{classList:{add:()=>{},remove:()=>{}}}},console,
  render:()=>vm.runInContext("if(typeof synchronizeOperationNotice==='function')synchronizeOperationNotice(state)",ctx)});
 vm.runInContext(source.slice(from,to)+`;this.setState=s=>state=s;this.getState=()=>state;this.execute=()=>run(async()=>{await cmd('transfer');notify('unconditional success');});this.show=notify;this.paint=s=>{state=s;render();};`,ctx);
 const pending={id:'journey-a',version:10,handoff_pending:true,automatic_recovery_enabled:true,
  handoff:{id:'operation-a',action:'transfer',status:'PENDING',message:'매장의 답을 확인하지 못했어요. 다시 확인해 주세요.',recovery:{state:'SCHEDULED'}}};
 ctx.setState(pending);ctx.api=async()=>pending;
 return {ctx,node,pending,timers:()=>scheduled};
}
const cases={
 'pending automatic notice is not a retry error':async h=>{await h.ctx.execute();assert.equal(h.node.className,'');assert.match(h.node.textContent,/자동/);assert.doesNotMatch(h.node.textContent,/다시 확인해 주세요/);},
 'same operation completion replaces pending error':async h=>{await h.ctx.execute();h.ctx.paint({...h.pending,version:11,handoff_pending:false,handoff:{...h.pending.handoff,status:'COMPLETED',message:'같은 주문으로 매장을 바꿨어요.'}});assert.equal(h.node.textContent,'같은 주문으로 매장을 바꿨어요.');assert.equal(h.node.className,'');},
 'rejection remains an error, not success':async h=>{await h.ctx.execute();h.ctx.paint({...h.pending,version:11,handoff_pending:false,handoff:{...h.pending.handoff,status:'REJECTED',message:'새 매장이 거절해 원래 주문을 유지했어요.'}});assert.match(h.node.textContent,/거절/);assert.equal(h.node.className,'error');},
 'review required is not auto success':async h=>{await h.ctx.execute();h.ctx.paint({...h.pending,version:11,handoff:{...h.pending.handoff,recovery:{state:'REVIEW_REQUIRED'},message:'연결이 오래 끊겨 추가 확인이 필요해요.'}});assert.match(h.node.textContent,/추가 확인/);assert.equal(h.node.className,'error');},
 'unrelated error is not overwritten by recovery':async h=>{await h.ctx.execute();h.ctx.show('비교 결과를 가져오지 못했어요.',true);h.ctx.paint({...h.pending,version:11,handoff_pending:false,handoff:{...h.pending.handoff,status:'COMPLETED',message:'이전 작업 완료'}});assert.equal(h.node.textContent,'비교 결과를 가져오지 못했어요.');assert.equal(h.node.className,'error');},
 'new journey does not retain old operation notice':async h=>{await h.ctx.execute();h.ctx.paint({...h.pending,id:'journey-b',version:1,handoff:null,handoff_pending:false});assert.equal(h.node.hidden,true);},
 'stale operation response cannot update newer notice':async h=>{await h.ctx.execute();const old=h.node.textContent;h.ctx.paint({...h.pending,version:9,handoff_pending:false,handoff:{...h.pending.handoff,status:'COMPLETED',message:'오래된 성공'}});assert.equal(h.node.textContent,old);},
 'unchanged pending polling does not restart toast timer':async h=>{await h.ctx.execute();const count=h.timers();h.ctx.paint({...h.pending,version:11});h.ctx.paint({...h.pending,version:12});assert.equal(h.timers(),count);},
 'manual recovery runtime retains manual guidance':async h=>{h.pending.automatic_recovery_enabled=false;await h.ctx.execute();assert.match(h.node.textContent,/다시 확인/);assert.equal(h.node.className,'error');},
 'real request error stays visible':async h=>{h.ctx.api=async()=>{const e=Error('서버가 요청을 처리하지 못했어요.');e.status=503;throw e;};await h.ctx.execute();assert.match(h.node.textContent,/서버가 요청/);assert.equal(h.node.className,'error');}
};
(async()=>{let failed=0;for(const [name,test] of Object.entries(cases)){try{await test(harness());console.log('PASS',name);}catch(e){failed++;console.error('FAIL',name,e.message);}}console.log(`${Object.keys(cases).length-failed}/${Object.keys(cases).length} operation notice checks passed`);process.exitCode=failed?1:0;})().catch(e=>{console.error(e);process.exit(1);});
