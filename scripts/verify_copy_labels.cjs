'use strict';
// Exercise presentation-only functions without a DOM or a fabricated response.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
function functions(path,start,end,exports){
  const source=fs.readFileSync(path,'utf8');
  const a=source.indexOf(start), b=source.indexOf(end,a);
  assert(a>=0 && b>a,path);
  const context=vm.createContext({});
  vm.runInContext(source.slice(a,b)+'\nthis.labels={'+exports.join(',')+'};',context);
  return context.labels;
}
const route=functions('demo/route/product.js','function friendlyRouteError','let state=',
  ['friendlyRouteError','routeEventTitle','routeReason','couponMessage']);
assert.match(route.friendlyRouteError({code:'STALE_QUOTE'},409),/최신 내용을/);
assert.match(route.friendlyRouteError({code:'BAD_CODE'},409),/수령 번호가 맞지/);
assert.match(route.friendlyRouteError('internal failure',500),/처리하지 못했어요/);
assert.equal(route.routeReason('BUDGET','old'),'결제 한도를 넘어요');
assert.equal(route.routeReason('UNKNOWN','원문'),'원문');
assert.match(route.couponMessage({coupon_reason:'STORE_MISMATCH'}),/이 매장에서는/);
const event={type:'BENEFITS_CONSUMED',title:'original',data:{amount:3700,points:1000}};
const before=JSON.stringify(event);assert.match(route.routeEventTitle(event),/사용과 적립/);
assert.equal(JSON.stringify(event),before);
const classic=functions('demo/index.html','function classicLabel','function showPage',
  ['classicLabel','classicDetail','classicTimelineLabel','classicError']);
assert.equal(classic.classicLabel('REVERSE_SETTLEMENT'),'정산 되돌리기');
assert.equal(classic.classicLabel('UNKNOWN_EVENT'),'UNKNOWN_EVENT');
assert.equal(classic.classicDetail('available_units=20'),'준비 가능한 양: 20');
assert.equal(classic.classicDetail('12:30 픽업 슬롯 2개 확보'),'12:30 픽업 자리 2개 확보');
assert.equal(classic.classicDetail('새 데모 세션을 만들었습니다.'),'새 체험을 시작했어요.');
assert.equal(classic.classicDetail('9800 KRW'),'9800 KRW');
assert.match(classic.classicError('internal traceback',500),/처리하지 못했어요/);
const review=functions('services/reconciler/app/repair_workbench.html','function repairError','function message',
  ['repairError','reviewAmount','reviewAuditDetail']);
assert.equal(review.reviewAmount({amount:3000,unit:'KRW'}),'3,000원');
assert.equal(review.reviewAmount({amount:90,unit:'PTS'}),'90P');
assert.match(review.repairError('stale plan; evidence has changed',409),/새 기록/);
// The labels object is deliberately supplied separately: normal application code owns it.
assert.match(review.repairError('unknown internal exception',500),/처리하지 못했어요/);
console.log('Presentation label checks passed; original records and numeric values preserved.');
