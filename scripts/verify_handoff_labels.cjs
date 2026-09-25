'use strict';
// Isolated async command-unit checks with explicit stub responses, not browser E2E.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('demo/route/product.js','utf8');
const start=source.indexOf('async function cmd('),end=source.indexOf('function labelIntent',start);
assert(start>=0&&end>start);
async function invoke(response,action){
  const ctx=vm.createContext({state:{id:'fixture',version:1},api:async()=>response,requestId:()=> 'fixture',render:()=>{}});
  vm.runInContext(source.slice(start,end)+';this.invoke=cmd;',ctx);
  return ctx.invoke(action);
}
(async()=>{
  await assert.rejects(()=>invoke({handoff_pending:true,handoff:{message:'처리 확인 중'}},'transfer'),/처리 확인 중/);
  await assert.rejects(()=>invoke({handoff:{status:'REJECTED',action:'transfer',message:'거절됨'}},'transfer'),/거절됨/);
  await assert.rejects(()=>invoke({handoff:{status:'REJECTED',action:'transfer',message:'거절됨'}},'recover'),/거절됨/);
  const result=await invoke({handoff:{status:'COMPLETED',action:'transfer'},order:{id:'same'}},'transfer');
  assert.equal(result.order.id,'same');
  // A later harmless clock command must not rethrow a prior rejected operation.
  await invoke({handoff:{status:'REJECTED',action:'transfer',message:'prior'}},'advance');
  console.log('5 async command UI checks passed; pending/rejected results cannot fall through to success toast.');
})().catch(e=>{console.error(e);process.exit(1);});
