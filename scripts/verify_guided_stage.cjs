'use strict';
const assert=require('node:assert/strict');
const {stageFor,PRESET}=require('../demo/route/guide-flow.js');
const base=()=>({order:{id:'one',state:'RESERVED',store_id:'wave'},handoff:null,handoff_pending:false,risk:{needs_attention:false}});
const cases=[
 ['no order',null,'reserve'],
 ['approval uncertain before order exists',{order:null,handoff_pending:true,pending_order_id:'pending'},'waiting'],
 ['first approval exhausted',{order:null,handoff_pending:true,handoff:{recovery:{state:'REVIEW_REQUIRED'}}},'review'],
 ['decline is not another automatic order',{order:null,handoff_pending:false,handoff:{status:'REJECTED'},payment:{state:'DECLINED'}},'declined'],
 ['original order',base(),'busy'],
 ['store delayed',{...base(),risk:{needs_attention:true}},'reject'],
 ['actual rejection',{...base(),handoff:{action:'transfer',status:'REJECTED'}},'disconnect'],
 ['rejected control is not a transfer rejection',{...base(),handoff:{action:'control_occupy',status:'REJECTED'}},'busy'],
 ['committed but not finalized is not success',{...base(),handoff_pending:true,handoff:{decision:'COMMIT',status:'PENDING',recovery:{state:'SCHEDULED'}}},'waiting'],
 ['retry exhaustion stays unresolved',{...base(),handoff_pending:true,handoff:{recovery:{state:'REVIEW_REQUIRED'}}},'review'],
 ['new store recorded',{...base(),order:{state:'RESERVED',store_id:'oat'}},'prepare'],
 ['preparing',{...base(),order:{state:'PREPARING',store_id:'oat'}},'ready'],
 ['ready',{...base(),order:{state:'READY',store_id:'oat'}},'claim'],
 ['picked up',{...base(),order:{state:'PICKED_UP',store_id:'oat'}},'receipt'],
 ['cancelled',{...base(),order:{state:'CANCELLED',store_id:'wave'}},'cancelled'],
 ['pending takes precedence over an old ready snapshot',{...base(),order:{state:'READY',store_id:'wave'},handoff_pending:true},'waiting']
];
for(const [name,input,want] of cases){const before=JSON.stringify(input);assert.equal(stageFor(input),want,name);assert.equal(JSON.stringify(input),before,'stage selection must not mutate server state');console.log('PASS',name);}
assert(Object.isFrozen(PRESET));assert.equal(PRESET.points,1000);assert.equal(PRESET.coupon_id,'welcome500');
console.log(`${cases.length} guided stage cases and preset checks passed`);
