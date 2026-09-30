/* Guided presentation. Every mutation still goes through the original HTTP API.
 * Progress is derived from persisted server state, never a scripted success. */
(() => {
  'use strict';
  const PRESET = Object.freeze({destination:'office',deadline_minutes:16,drink:'latte',
    milk:'regular',decaf:false,budget:5500,max_detour:5,priority:'arrival',
    coupon_id:'welcome500',points:1000});
  function stageFor(s) {
    if (s?.handoff_pending) return s.handoff?.recovery?.state === 'REVIEW_REQUIRED' ? 'review' : 'waiting';
    if (!s?.order) return s?.payment?.state==='DECLINED' || s?.handoff?.status==='REJECTED' ? 'declined' : 'reserve';
    const o=s.order;
    if (o.state==='CANCELLED') return 'cancelled';
    if (o.state==='PICKED_UP') return 'receipt';
    if (o.state==='READY') return 'claim';
    if (o.state==='PREPARING') return 'ready';
    if (o.store_id!=='wave') return 'prepare';
    if (s.handoff?.action==='transfer' && s.handoff.status==='REJECTED') return 'disconnect';
    return s.risk?.needs_attention ? 'reject' : 'busy';
  }
  if (typeof module==='object' && module.exports) {
    module.exports={stageFor,PRESET}; return;
  }
  const key='pickup-pact.guided-journey';
  const text={
    declined:['01 / 승인 거절','가상 카드 승인이 거절됐어요.','주문이나 혜택을 소모하지 않았어요. 상세 설정에서 다른 가상 카드로 바꾼 뒤 다시 시도할 수 있어요.','주문·결제 결과 확인'],
    reserve:['01 / 주문','주문할 매장을 확인해요.','서버가 계산한 웨이브의 금액과 자리를 확인한 뒤 주문해요.','웨이브에서 주문하기'],
    busy:['02 / 문제 만들기','쿠폰과 포인트를 적용했어요.','이제 주문한 매장이 바빠지는 상황을 만들어 보세요. 실제 매장의 주문에는 영향이 없어요.','매장이 밀리면?'],
    reject:['03 / 거절 확인','다른 매장이 거절하면 어떻게 될까요?','새 매장 거절을 설정하고 변경 조건을 확인해요. 확인 버튼을 눌러야 요청을 보내요.','새 매장 거절 체험'],
    disconnect:['04 / 응답 끊김','거절됐지만 원래 주문과 혜택은 남았어요.','이번에는 새 자리를 확보한 뒤 응답만 끊겨요. 변경 조건에 동의한 뒤 서버의 자동 확인을 살펴보세요.','응답 끊김 체험'],
    waiting:['04 / 서버가 확인 중','다시 주문하지 않아도 돼요.','이 화면을 닫아도 서버가 저장된 작업을 다시 확인해요. 확인 전에는 성공으로 표시하거나 새 제조를 시작하지 않아요.','서버가 확인하고 있어요'],
    review:['04 / 추가 확인 필요','아직 연결을 확인하지 못했어요.','원래 주문을 임의로 풀거나 새 결제로 처리하지 않았어요. 아래 저장된 작업에서 처리 결과를 다시 확인할 수 있어요.','저장된 작업 보기'],
    prepare:['05 / 매장에서 준비','같은 주문으로 새 매장에 연결됐어요.','이제 매장 화면에서 커피를 만들어요. 기다리지 않도록 체험 시간을 제조 시작 시각까지 앞당겨요.','매장에서 커피 만들기'],
    ready:['05 / 매장에서 준비','새 매장이 커피를 만들고 있어요.','체험 시간을 준비 완료 시각까지 앞당겨요. 제조를 시작한 뒤에는 매장을 다시 바꾸지 않아요.','커피 준비 완료'],
    claim:['06 / 수령','커피가 준비됐어요.','내 주문의 수령 번호로 확인해요. 이 버튼도 기존 수령 API를 사용하며 모의 결제는 한 번만 확정해요.','수령 번호로 커피 받기'],
    receipt:['06 / 영수증','주문부터 수령까지 이어졌어요.','같은 주문 번호와 매장 변경 내역, 쿠폰·포인트 사용, 모의 결제 기록을 영수증에서 확인하세요.','영수증 보기'],
    cancelled:['체험 종료','주문을 취소했어요.','이 주문은 다시 만들거나 자동으로 옮기지 않아요. 취소와 혜택 복원 기록을 확인하세요.','영수증 보기']
  };
  const start=$('quickStart'),panel=$('guidePanel'),button=$('guideAction');
  let stage=null, initialized=false;
  function isGuided(s) { return !!s && localStorage.getItem(key)===s.id; }
  function renderGuide(s) {
    const guided=isGuided(s);
    document.body.classList.toggle('guided-flow',guided);
    document.body.classList.toggle('entry-home',!s?.order);
    panel.hidden=!guided;
    $('quickEntry').hidden=!!s || !!localStorage.getItem('pickup-pact.route-journey');
    if(!guided)return;
    stage=stageFor(s);panel.dataset.stage=stage;
    const row=text[stage];
    $('guideStep').textContent=row[0];$('guideTitle').textContent=row[1];$('guideText').textContent=row[2];
    button.textContent=row[3];button.disabled=stage==='waiting';
    const o=s.order;
    $('guideOrderId').textContent=o?.id||s.pending_order_id||'아직 주문 전';
    $('guideStore').textContent=o?.store_name||'매장 확인 중';
    $('guidePoints').textContent=(s.wallet?.held_points||s.wallet?.spent||0).toLocaleString('ko-KR')+'P';
    $('guidePayment').textContent=s.handoff_pending?'결과 확인 중':(s.receipt?.capture_count||0)+'건';
    $('guideProof').hidden=!o&&!s.pending_order_id;
    if(s.handoff_pending){$('guideTitle').textContent=s.payment?.state==='CONFIRMING_APPROVAL'?'승인 결과를 확인하고 있어요.':'매장·결제 기록을 확인하고 있어요.';}
    // Extra controls stay available, but do not crowd the guided next action.
    const controls=document.querySelector('.handoff-controls');
    if(controls)controls.open=false;
  }
  async function reservePreset() {
    const p=state.recommendations.find(p=>p.store_id==='wave');
    if(!p)throw Error('지금은 이 조건으로 웨이브에 주문할 수 없어요. 직접 조건을 골라주세요.');
    await cmd('reserve',{quote_id:p.quote_id});
  }
  start.addEventListener('click',()=>run(async()=>{
    // An unresolved saved reference is not permission to create another order.
    if(!initialized || state || localStorage.getItem('pickup-pact.route-journey'))return;
    start.disabled=true;
    try {
      state=await api('/api/route/journeys',{...PRESET,...(window.pickupPaymentOptions?.()||{})});
      localStorage.setItem(key,state.id);
      render();
      await reservePreset();
      panel.scrollIntoView({behavior:'smooth',block:'start'});
    } finally {
      if(!state && !localStorage.getItem('pickup-pact.route-journey'))start.disabled=false;
    }
  }));
  $('openCustomOrder').addEventListener('click',()=>{
    const details=$('customizeOrder');details.open=true;
    details.scrollIntoView({behavior:'smooth',block:'start'});$('destination').focus({preventScroll:true});
  });
  button.addEventListener('click',()=>run(async()=>{
    if(!isGuided(state) || state.handoff_pending && stage!=='review')return;
    const current=stageFor(state);
    if(current==='reserve'){await reservePreset();return;}
    if(current==='busy'){await cmd('disrupt',{store_id:state.order.store_id,minutes:12});return;}
    if(current==='reject'||current==='disconnect'){
      state=await api(`/api/route/journeys/${state.id}/transfer-controls`,{
        action:'fault',fault:current==='reject'?'target_reject':'after_target_hold',
        expected_version:state.version,request_id:requestId()});
      render();
      const plan=state.recommendations.find(p=>p.store_id==='oat');
      if(!plan)throw Error('현재 조건에 맞는 새 매장이 없어요. 주문은 그대로 두었어요.');
      // Reuse the actual consent dialog, including server-provided terms/prices.
      switchView('customer');transferDialog(plan);return;
    }
    if(current==='prepare'||current==='ready'){
      switchView('merchant');
      const target=current==='prepare'?state.current_plan.start_at:state.order.ready_at;
      let remaining=Math.max(0,target-state.clock);
      while(remaining>0){const amount=Math.min(30,remaining);await cmd('advance',{minutes:amount});remaining-=amount;}
      await cmd(current==='prepare'?'start':'ready');return;
    }
    if(current==='claim'){
      switchView('customer');
      const code=$('pickupCode')?.textContent.trim();
      if(!code)throw Error('수령 번호를 아직 확인하지 못했어요. 내 주문을 확인해 주세요.');
      $('claimCode').value=code;await cmd('claim',{pickup_code:code});return;
    }
    if(current==='receipt'||current==='cancelled'){switchView('receipt');return;}
    if(current==='declined'){document.getElementById('paymentEvidence').scrollIntoView({behavior:'smooth'});return;}
    if(current==='review'){$('handoffPanel').scrollIntoView({behavior:'smooth',block:'start'});}
  }));
  document.addEventListener('route:render',e=>renderGuide(e.detail));
  // Do not race the original saved-order restoration or its deferred listeners.
  const began=Date.now();
  function waitForEntry() {
    if(!routeBootFinished){
      if(Date.now()-began<20000){setTimeout(waitForEntry,100);return;}
      start.textContent='서버 연결을 확인하지 못했어요';return;
    }
    initialized=!!catalog;
    const saved=localStorage.getItem('pickup-pact.route-journey');
    start.disabled=!initialized||!!saved||!!state;
    if(saved&&!state){
      start.textContent='저장된 주문을 먼저 확인해 주세요';
      $('quickStartSummary').textContent='이전 주문 연결은 지우지 않았어요. 새로고침 후 다시 확인해 주세요.';
    }
    renderGuide(state);
  }
  document.addEventListener('DOMContentLoaded',waitForEntry,{once:true});
  if(document.readyState==='complete')waitForEntry();
})();
