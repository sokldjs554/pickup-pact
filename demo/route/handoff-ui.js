/* Recovery display uses persisted server results, never a scripted success. */
(() => {
  const titleByState = {RESERVED:'주문 유지',FROZEN:'제조 잠시 멈춤',HELD:'자리 확인 중',
    PREPARING:'제조 중',READY:'준비 완료',RELEASED:'자리 반환',ABORTED:'보류 해제',
    CANCELLED:'취소됨',CLAIMED:'수령 완료'};
  const faultNames={none:'문제 없이 변경',target_reject:'새 매장에서 거절',
    after_target_hold:'새 매장 수락 뒤 응답 끊김',after_source_release:'원래 매장 해제 뒤 응답 끊김',
    after_target_activation:'새 매장 인계 뒤 응답 끊김'};
  const steps=['FREEZE','HOLD','DECIDE','RELEASE_SOURCE','ACTIVATE','FINALIZE'];
  const labels=['원래 매장 대기','새 자리 확보','변경 내용 저장','기존 자리 반환','새 매장 연결','주문·혜택 확인'];
  let comparison=null, comparisonPending=false;
  const controlDrafts=new PickupSelectionState();
  const main=document.querySelector('.main-column');
  const assurance=document.createElement('section');assurance.id='handoffPanel';assurance.hidden=true;
  $('orderArea').insertAdjacentElement('afterend',assurance);
  const evidence=document.createElement('section');evidence.id='handoffComparison';evidence.className='handoff-comparison panel';
  evidence.innerHTML='<div class="eyebrow">취소하고 다시 주문하는 것과 무엇이 다를까요?</div><h2>안 되면, 원래 주문은 남을까요?</h2><p>정상 처리부터 거절·응답 끊김까지 같은 조건으로 실행해 보세요. 기존 매장 유지도 함께 비교해요.</p><button type="button" id="compareHandoff" class="primary">네 가지 방식 직접 비교</button><div id="handoffComparisonResult" aria-live="polite"></div>';
  main.append(evidence);


  function lockPending(s){
    if(!s?.handoff_pending)return;
    $('routeArea').hidden=true;
    const comparison=document.querySelector('#orderComparison');if(comparison)comparison.hidden=true;
    $('mapTitle').textContent='매장 변경 결과를 확인하고 있어요.';
    for(const b of document.querySelectorAll('#orderArea [data-action],#merchantContent [data-action],#claim')){
      if(b.dataset.action!=='refresh'){b.disabled=true;b.title='먼저 매장 변경 결과를 다시 확인해 주세요.';}
    }
    const pill=document.querySelector('#orderArea .status-pill');if(pill)pill.textContent='처리 확인 중';
    const banner=document.querySelector('#orderArea .rescue-banner');
    if(banner)banner.innerHTML='<h3>매장에 처리 결과를 확인하고 있어요.</h3><p>아직 완료됐다고 판단하지 않아요. 아래에서 같은 작업을 다시 확인해 주세요.</p>';
    const success=document.querySelector('#orderArea .success-banner');if(success)success.hidden=true;
    const merchant=document.querySelector('#merchantContent .ticket-status');
    if(merchant)merchant.textContent='매장 간 확인이 끝날 때까지 새 제조나 취소는 진행하지 않아요.';
  }
  document.addEventListener('route:aux-render',e=>lockPending(e.detail));
  document.addEventListener('route:render',e=>{
    const s=e.detail,order=s.order,op=s.handoff;
    controlDrafts.begin(s.id);
    assurance.hidden=!order&&!s.handoff_pending;
    if(s.handoff_pending||op?.status==='REJECTED')$('orderArea').insertAdjacentElement('beforebegin',assurance);
    else $('orderArea').insertAdjacentElement('afterend',assurance);
    if(assurance.hidden)return;
    if(!s.world_id){
      assurance.innerHTML='<div class="handoff-card"><h3>이전 체험의 주문이에요.</h3><p>새로운 매장 변경을 보려면 상단에서 새 체험을 시작해 주세요. 기존 기록은 바꾸지 않았어요.</p></div>';return;
    }
    const transfer=op?.action==='transfer';
    const status=transfer?op.status:s.handoff_pending?'PENDING':'IDLE';
    const heading=status==='PENDING'?'응답이 없어도, 새 주문을 만들지 않아요.':status==='REJECTED'?'바꾸지 못했지만, 원래 주문은 남았어요.':status==='COMPLETED'?'새 매장이 받았어요. 주문은 하나예요.':'다른 매장이 받을 때만 바꿔요.';
    const message=transfer||s.handoff_pending?op.message:'새 자리를 확보하기 전에 기존 주문을 취소하지 않아요. 확인 중에는 두 매장이 동시에 만들지 못하게 막아요.';
    const seen=new Set((op?.history||[]).filter(h=>h.result!=='REPLY_NOT_CONFIRMED').map(h=>h.phase));
    let html=`<article class="handoff-card ${status.toLowerCase()}"><span class="eyebrow">주문을 잃지 않는 매장 변경</span><h2>${esc(heading)}</h2><p class="handoff-message" id="handoffMessage">${esc(message)}</p>`;
    if(transfer){html+=`<ol class="handoff-steps">${steps.map((step,i)=>`<li class="${seen.has(step)?'checked':op.phase===step?'current':''}"><span>${seen.has(step)?'✓':i+1}</span>${labels[i]}</li>`).join('')}</ol>`;}
    if(order){
      const places=Object.entries(s.merchant_capacity||{}).flatMap(([id,v])=>v.reservations.filter(r=>r.order_id===order.id).map(r=>({id,...r})));
      html+=`<div class="handoff-facts"><div><span>주문 번호</span><b>${esc(order.id)}</b></div><div><span>적용 중인 포인트</span><b>${s.wallet.held_points.toLocaleString('ko-KR')}P</b></div></div><div class="handoff-seats">${places.map(r=>`<span>${esc(catalog?.stores.find(v=>v.id===r.id)?.name||r.id)} <b>${titleByState[r.phase]||esc(r.phase)}</b></span>`).join('')}</div>`;
    }
    if(s.handoff_pending){
      html+='<p class="handoff-note">위 주문 정보는 최종 확인 전이에요. 처리 결과가 불명확한 동안에는 새 제조·취소·포인트 사용을 막아요.</p><button class="primary" id="recoverHandoff">같은 작업 다시 확인하기</button>';
    }else if(order?.state==='RESERVED'){
      const others=s.stores.filter(p=>p.id!==order.store_id);
      const target=s.recommendations[0]?.store_id||others[0]?.id;
      html+=`<details class="handoff-controls" open><summary>거절·응답 끊김·마지막 자리 경쟁을 체험해 보세요</summary><div class="handoff-field"><label for="handoffFault">다음 매장 변경에서 생길 상황</label><select id="handoffFault">${Object.entries(faultNames).map(([v,label])=>`<option value="${v}" ${controlDrafts.value('fault',s.next_transfer_fault||'none')===v?'selected':''}>${label}</option>`).join('')}</select><button type="button" class="secondary" id="setHandoffFault">이 상황 적용</button></div><div class="handoff-field"><label for="handoffTarget">다른 손님이 먼저 주문한다면?</label><select id="handoffTarget">${others.map(v=>`<option value="${v.id}" ${v.id===controlDrafts.value('target',target)?'selected':''}>${esc(v.name)} · 남은 자리 ${s.merchant_capacity[v.id]?.available??'확인 중'}/${s.merchant_capacity[v.id]?.capacity??'?'}</option>`).join('')}</select><div class="handoff-control-buttons"><button type="button" class="secondary" id="occupyHandoff">다른 손님 주문 추가</button><button type="button" class="secondary" id="clearHandoff">다른 손님 주문 취소</button></div></div><p class="handoff-note">이 체험 회차 안에서만 제조 자리를 공유해요. 실제 가맹점이나 다른 방문자의 주문은 바뀌지 않아요.</p></details>`;
    }
    assurance.innerHTML=html+'</article>';
    lockPending(s);
    $('handoffFault')?.addEventListener('change',e=>controlDrafts.choose('fault',e.target.value));
    $('handoffTarget')?.addEventListener('change',e=>controlDrafts.choose('target',e.target.value));
    $('recoverHandoff')?.addEventListener('click',()=>run(async()=>{await cmd('recover');notify('저장된 작업을 이어서 확인했어요. 주문 결과를 확인하세요.');}));
    async function control(body){
      state=await api(`/api/route/journeys/${state.id}/transfer-controls`,{expected_version:state.version,request_id:requestId(),...body});render();
      if(state.handoff_pending){notify('매장에 처리 결과를 확인하고 있어요. 자동으로 다시 확인할게요.');return false;}
      if(state.handoff?.action?.startsWith('control_')&&state.handoff.status==='REJECTED'){notify(state.handoff.message,true);return false;}
      return true;
    }
    $('setHandoffFault')?.addEventListener('click',()=>run(async()=>{
      const fault=$('handoffFault').value;if(!await control({action:'fault',fault}))return;controlDrafts.applied('fault',fault);
      notify(fault==='none'?'문제 없는 상황으로 바꿨어요.':'다음 매장 변경에서 '+faultNames[fault]+' 상황을 확인해 보세요.');
    }));
    $('occupyHandoff')?.addEventListener('click',()=>run(async()=>{if(!await control({action:'occupy',store_id:$('handoffTarget').value}))return;notify('같은 제조 자리에 다른 손님의 주문을 추가했어요.');}));
    $('clearHandoff')?.addEventListener('click',()=>run(async()=>{if(!await control({action:'clear',store_id:$('handoffTarget').value}))return;notify('추가했던 손님의 자리를 돌려줬어요.');}));
    for(const card of document.querySelectorAll('.route-card')){
      const capacity=s.merchant_capacity[card.dataset.hover];
      if(capacity)card.insertAdjacentHTML('beforeend',`<div class="handoff-availability">현재 남은 제조 자리 <b>${capacity.available}/${capacity.capacity}</b> · 변경할 때 다시 확인해요</div>`);
    }
  });

  function outcome(row){
    const title=!row.has_order?'진행할 주문 없음':row.original_preserved?'기존 매장에서 주문 유지':row.same_order?'같은 주문으로 매장 변경':'새 주문으로 접수';
    return `<b class="${row.has_order?'outcome-kept':'outcome-empty'}">${title}</b><span>${row.has_order?'결제 예정 '+won(row.cash_due):'새 주문·결제 없음'}</span><small>실행 요청 ${row.customer_commands}회${row.same_request_retries?' · 동일 요청 재확인 '+row.same_request_retries+'회 포함':''}</small>`;
  }
  async function fetchComparison(intent,signal){
    // A timeout is safe here because this endpoint executes isolated comparison
    // worlds; do not apply this cancellation policy to order mutation requests.
    const response=await fetch('/api/route/transfer-comparison',{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({intent}),signal
    });
    const payload=await response.json();
    if(!response.ok){
      const error=new Error(friendlyRouteError(payload.detail,response.status));
      error.status=response.status;throw error;
    }
    return payload;
  }
  $('compareHandoff').addEventListener('click',async()=>{
    if(comparisonPending)return;
    const button=$('compareHandoff'),result=$('handoffComparisonResult');
    const intent=JSON.parse(JSON.stringify(state?.intent||{destination:$('destination').value,deadline_minutes:Number($('deadline').value),
      drink,milk:$('oat').checked?'oat':'regular',decaf:$('decaf').checked,budget:Number($('budget').value),
      max_detour:Number($('detour').value),priority:$('priority').value,coupon_id:$('coupon').value||null,points:Number($('points').value)||0}));
    // This is a read-only, isolated comparison: do not hold the order-command
    // lock or silently use conditions changed while the response is pending.
    comparisonPending=true;button.disabled=true;button.textContent='비교 결과 확인 중…';
    result.setAttribute('aria-busy','true');
    result.innerHTML='<p class="handoff-note" role="status">6가지 상황을 실제 주문 코드로 비교하고 있어요. 잠시만 기다려 주세요. 내 주문은 그대로 이용할 수 있어요.</p>';
    const controller=new AbortController();
    const timeout=setTimeout(()=>controller.abort(),35000);
    try{
    comparison=await fetchComparison(intent,controller.signal);
    $('handoffComparisonResult').innerHTML=`<p class="handoff-note">비교를 시작할 때의 조건: ${esc(labelIntent(intent))} · ${time(intent.deadline_minutes)}까지 · 할인 후 결제 한도 ${won(intent.budget)}</p><p class="handoff-note">${esc(comparison.disclosure)}</p><p class="handoff-note">${esc(comparison.baseline_design||'')}</p><div class="handoff-table-wrap"><table><thead><tr><th>같은 상황</th><th>취소 후 다시 주문</th><th>새 자리 확보 후 재주문</th><th>같은 주문 이어가기</th></tr></thead><tbody>${comparison.cases.map(r=>`<tr><th scope="row">${esc(r.label)}</th><td>${outcome(r.cancel_reorder)}</td><td>${outcome(r.reserve_first_reorder)}</td><td>${outcome(r.guarded_transfer)}</td></tr>`).join('')}</tbody></table></div><p class="handoff-note">실행 요청 수는 서버 명령 횟수이며 사용자의 클릭이나 소요 시간을 측정한 값은 아니에요. 정상 처리에서는 예상 도착 시간과 금액이 같을 수 있어요. 같은 안전 절차로 새 자리를 먼저 확보하는 재주문도 원래 주문을 지킬 수 있어요. 정상적인 성공에서는 금액·도착 예상이 같고, 주문 번호 유지와 재승인 기록이 달라져요. 쿠폰 기간 사례만 비교를 위해 전 매장 정률 쿠폰과 90분 마감을 사용해요.</p><details><summary>기존 매장 유지 결과도 보기</summary><div class="handoff-stay">${comparison.cases.map(r=>`<p><b>${esc(r.label)}</b><span>${r.stay.has_order?time(r.stay.predicted_arrival)+' 도착 예상 / '+won(r.stay.cash_due):'처음부터 주문 불가'}</span></p>`).join('')}</div></details><button type="button" class="secondary" id="downloadHandoffComparison">조건·전체 실행 기록 받기</button>`;
    $('downloadHandoffComparison').addEventListener('click',()=>{
      const url=URL.createObjectURL(new Blob([JSON.stringify(comparison,null,2)],{type:'application/json'}));
      const a=document.createElement('a');a.href=url;a.download='pickup-pact-transfer-comparison.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
    });
    notify('네 가지 방식을 같은 상황에서 실행했어요. 같았던 결과와 실패한 결과도 함께 확인하세요.');
    }catch(error){
      const message=error.name==='AbortError'
        ? '비교 결과를 아직 받지 못했어요. 주문은 바뀌지 않았어요. 잠시 후 다시 비교해 주세요.'
        : error.status===429 ? error.message : '비교 결과를 불러오지 못했어요. 내 주문은 그대로예요. 잠시 후 다시 눌러주세요.';
      result.innerHTML=`<p class="handoff-note" role="alert">${esc(message)}</p>`;
      notify(message,true);
    }finally{
      clearTimeout(timeout);comparisonPending=false;button.disabled=false;
      button.textContent='네 가지 방식 직접 비교';result.setAttribute('aria-busy','false');
    }
  });
})();
