/* Read-only independent evidence. Neither a timer nor a callback creates success. */
(() => {
  'use strict';
  function acceptsEvidence(result, current) {
    return !!current && result?.journey_id === current.id && result?.journey_version === current.version;
  }
  function headline(result) {
    if(!result) return '독립 서버의 자료를 읽지 못했어요. 확인 전에는 완료로 판단하지 않아요.';
    if(result.status==='UNAVAILABLE') return '일부 원본을 읽지 못했어요. 청구 0건이나 정상으로 판단하지 않아요.';
    if(result.status==='MISMATCH') return '서버 기록이 서로 달라 추가 확인이 필요해요.';
    if(result.status==='PENDING') return '저장된 거래와 주문 결과를 확인하고 있어요. 다시 결제하지 마세요.';
    return result.terminal ? '주문·매장·결제·혜택의 기록이 일치해요.' : '현재 기록이 일치해요. 아직 수령 전이에요.';
  }
  if(typeof module==='object' && module.exports){module.exports={acceptsEvidence,headline};return;}
  const byId=id=>document.getElementById(id);
  const panel=byId('paymentEvidence');
  const statusText={MATCH:'일치',PENDING:'확인 중',MISMATCH:'불일치',UNAVAILABLE:'자료 없음'};
  const phaseText={RESERVED:'접수 · 제조 가능',HELD:'새 자리 보류 · 제조 불가',FROZEN:'변경 확인 중 · 제조 정지',
    PREPARING:'제조 중',READY:'수령 대기',CLAIMED:'수령 완료',CANCELLED:'취소',RELEASED:'이전 매장 자리 해제',ABORTED:'새 자리 정리'};
  const financialText={AUTHORIZE:'승인 보류',CAPTURE:'청구 확정',VOID:'승인 해제'};
  const allowed=new Set(['none','authorize_reply_lost','capture_reply_lost','void_reply_lost','notification_duplicate','notification_late']);
  const preset=new URLSearchParams(location.search).get('payment');
  if(allowed.has(preset))byId('paymentScenario').value=preset;
  window.pickupPaymentOptions=()=>({payment_card:byId('paymentCard').value,payment_scenario:byId('paymentScenario').value});
  let current=null,last=null,controller=null,serial=0,timer=null;
  const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
  const money=n=>n.toLocaleString('ko-KR')+'원';
  function paint(result){
    byId('paymentStatus').textContent=headline(result);
    panel.dataset.status=result?.status||'UNAVAILABLE';
    const facts=byId('paymentFacts');facts.replaceChildren();
    const pg=result?.payment;
    for(const [label,value] of [['확정 청구',pg?money(pg.captured_krw):'확인 불가'],['승인 보류 잔액',pg?money(pg.held_krw):'확인 불가'],['확정 거래 수',pg?pg.capture_count+'건':'확인 불가']]){
      const item=el('div');item.append(el('span',label),el('strong',value));facts.append(item);
    }
    const transactions=byId('paymentTransactions');transactions.replaceChildren();
    if(pg){
      if(!pg.transactions.length)transactions.append(el('p','원본 결제 서버에서 조회한 거래가 아직 없어요.'));
      for(const row of pg.transactions){
        const line=el('div',undefined,'payment-transaction');
        line.append(el('b',financialText[row.kind]||row.kind),el('span',money(row.amount_krw)),el('small',row.authorization_id));transactions.append(line);
      }
    }else transactions.append(el('p','결제 서버의 원본을 확인할 수 없어요. 주문 서버의 기록으로 대신하지 않아요.'));
    byId('evidenceTimestamp').textContent=result?`조회 시각 ${new Date(result.observation.finished_at*1000).toLocaleTimeString('ko-KR')} · 결제 기록 순번 ${result.observation.payment_revision??'미확인'} · ${result.observation.stable?'조회 구간 일치':'조회 중 변화 또는 미확인'}`:'조회 시각을 확인하지 못했어요.';
    byId('downloadPaymentEvidence').disabled=!result;
    const terminals=byId('merchantTerminals');terminals.hidden=false;terminals.replaceChildren();
    const source=current?.events?.find(e=>e.type==='ORDER_RESERVED')?.data?.store_id;
    const shops=[...new Set([source||current?.handoff?.source||current?.order?.store_id||'wave',current?.handoff?.target||current?.order?.store_id||'oat'])];
    if(shops.length===1)shops.push(shops[0]==='oat'?'wave':'oat');
    for(const shop of shops){
      const entry=result?.merchants?.[shop],seat=entry?.reservation;
      const card=el('article',undefined,'terminal-card');card.dataset.shop=shop;
      const name=current?.stores?.find(s=>s.id===shop)?.name||shop;
      card.append(el('span','가상 가맹점 단말 · HTTP 조회','eyebrow'),el('h3',name),el('b',!entry?'자료를 읽지 못함':seat?phaseText[seat.phase]:'이 주문 접수 없음','terminal-phase'));
      card.append(el('p',seat?`${seat.order_id} · 주문 세대 ${seat.generation}`:result?.order_id||'주문 확인 중'));
      const receipt=entry?.last_receipt;
      card.append(el('small',receipt?`마지막 명령 ${receipt.action} · ${receipt.result.ok?'처리 확인':receipt.result.code}`:'이 주문의 처리 영수증 없음'));
      if(seat?.phase==='RESERVED')card.append(el('p',current?.handoff_pending?'변경·결제 확인 중에는 제조하지 않아요.':'아래 주문 조정 화면을 통해 제조를 시작해요.'));
      terminals.append(card);
    }
    const proof=byId('finalProof');proof.hidden=false;proof.dataset.status=result?.status||'UNAVAILABLE';
    byId('finalProofState').textContent=headline(result);
    const checks=byId('finalProofChecks');checks.replaceChildren();
    for(const key of ['order','merchant','payment','benefits']){
      const check=result?.checks?.[key];const card=el('article',undefined,'proof-check');
      card.dataset.check=key;card.dataset.status=check?.status||'UNAVAILABLE';
      card.append(el('span',statusText[check?.status||'UNAVAILABLE'],'check-label'),el('h3',check?.label||({order:'주문',merchant:'매장',payment:'결제',benefits:'혜택'})[key]),el('p',check?.detail||'원본을 다시 확인해야 해요.'));checks.append(card);
    }
  }
  function schedule(){
    clearTimeout(timer);
    if(current?.protocol_version===2 && !document.hidden && (current.handoff_pending || !last || last.status==='UNAVAILABLE' || last.status==='PENDING'))timer=setTimeout(refresh,2500);
  }
  async function refresh(){
    clearTimeout(timer);
    if(!current||current.protocol_version!==2)return;
    controller?.abort();const active=new AbortController();controller=active;
    const ticket=++serial,id=current.id;const watchdog=setTimeout(()=>active.abort(),12000);
    byId('refreshPaymentEvidence').disabled=true;
    try{
      const response=await fetch(`/api/route/journeys/${encodeURIComponent(id)}/reconciliation`,{signal:active.signal,cache:'no-store'});
      if(!response.ok)throw Error('evidence not available');
      const result=await response.json();
      if(ticket!==serial)return;
      if(!current||current.id!==id||result.journey_id!==id||!Number.isSafeInteger(result.journey_version))throw Error('evidence identity not confirmed');
      if(result.journey_version>current.version){
        // Another tab may have finished a command while this tab was idle.
        // Reconcile its new coordinator version with a GET; never retain the
        // old green proof or replay the command that changed the order.
        last=null;paint(null);byId('paymentStatus').textContent='다른 화면에서 바뀐 주문을 다시 확인하고 있어요.';
        const updated=await fetch(`/api/route/journeys/${encodeURIComponent(id)}`,{signal:active.signal,cache:'no-store'});
        if(!updated.ok)throw Error('latest order unavailable');
        const newer=await updated.json();
        if(ticket!==serial)return;
        if(newer.id!==id||!Number.isSafeInteger(newer.version)||newer.version<result.journey_version||state?.id!==id)throw Error('latest order identity not confirmed');
        state=newer;render();return;
      }
      if(!acceptsEvidence(result,current))throw Error('evidence revision not confirmed');
      last=result;paint(result);
    }catch(error){
      if(ticket===serial){last=null;paint(null);}
    }finally{
      clearTimeout(watchdog);
      if(ticket===serial){byId('refreshPaymentEvidence').disabled=false;schedule();}
    }
  }
  function renderPayment(s){
    const changed=!current||s.id!==current.id||s.version!==current.version;
    current=s;panel.hidden=s.protocol_version!==2;
    if(panel.hidden){controller?.abort();clearTimeout(timer);byId('merchantTerminals').hidden=true;byId('finalProof').hidden=true;return;}
    byId('applyPaymentFault').disabled=!!s.handoff_pending||['PICKED_UP','CANCELLED'].includes(s.order?.state);
    byId('retryApprovedCard').hidden=!!s.order||!!s.handoff_pending||s.payment?.state!=='DECLINED';
    if(changed){last=null;paint(null);
      // Use Korean copy while never reusing an old green result for a new version.
      byId('paymentStatus').textContent='바뀐 주문과 원본 기록을 다시 확인하고 있어요.';refresh();
    }else if(last)paint(last);
  }
  document.addEventListener('route:render',e=>renderPayment(e.detail));
  document.addEventListener('route:aux-render',e=>{if(current?.id===e.detail.id&&last)paint(last);});
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();else clearTimeout(timer);});
  byId('refreshPaymentEvidence').addEventListener('click',refresh);
  byId('applyPaymentFault').addEventListener('click',()=>run(async()=>{
    if(!state||state.protocol_version!==2||state.handoff_pending)return;
    state=await api(`/api/route/journeys/${state.id}/payment-controls`,{action:'fault',fault:byId('nextPaymentFault').value,expected_version:state.version,request_id:requestId()});
    render();notify('선택한 상황을 다음 해당 결제 단계에 적용했어요.');
  }));
  byId('retryApprovedCard').addEventListener('click',()=>run(async()=>{
    const target=state.handoff?.target||'wave';
    state=await api(`/api/route/journeys/${state.id}/payment-controls`,{action:'card',card_token:'demo-approved',expected_version:state.version,request_id:requestId()});
    render();const p=state.recommendations.find(p=>p.store_id===target);
    if(!p)throw Error('현재 조건으로 주문할 수 없어요. 직접 조건을 다시 확인해 주세요.');
    await cmd('reserve',{quote_id:p.quote_id});
  }));
  byId('downloadPaymentEvidence').addEventListener('click',()=>{
    if(!last||!acceptsEvidence(last,current))return;
    const url=URL.createObjectURL(new Blob([JSON.stringify(last,null,2)],{type:'application/json'}));
    const a=document.createElement('a');a.href=url;a.download=`pickup-pact-${last.order_id||last.journey_id}-evidence.json`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  });
})();
