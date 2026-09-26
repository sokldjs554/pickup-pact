/* Render the accepted server quote and settlement; never calculate new terms. */
(() => {
  const spec=p=>`${p.drink==='latte'?'카페라떼':'아메리카노'} · ${p.volume_ml}mL · ${p.temperature==='ICED'?'아이스':'따뜻하게'}${p.milk==='oat'?' · 오트':''}${p.decaf?' · 디카페인':''}`;
  function allocation(m,title){
    return `<details class="agreement-cost"><summary>${esc(title)}</summary><dl><div><dt>고객 모의 결제</dt><dd>${won(m.customer_cash)}</dd></div><div><dt>전 매장 쿠폰 · 플랫폼 부담</dt><dd>${won(m.platform_coupon)}</dd></div><div><dt>사용 포인트 · 플랫폼 부담</dt><dd>${won(m.platform_points)}</dd></div><div><dt>매장 전용 할인 · 해당 매장 부담</dt><dd>${won(m.merchant_coupon)}</dd></div><div class="agreement-total"><dt>수령 매장의 모의 수취액</dt><dd>${won(m.merchant_receivable)}</dd></div></dl><p>고객 결제 + 플랫폼 쿠폰·포인트 부담 = 매장 수취액이에요. 실제 가맹점 정산이 아닌 데모 정책이며, 새로 적립한 포인트는 이번 결제액에 더하지 않아요.</p></details>`;
  }
  document.addEventListener('route:transfer-preview',e=>{
    const t=e.detail.plan.transfer_terms;
    if(!t){$('dialogBody').insertAdjacentHTML('beforeend','<p class="agreement-warning">이전 체험의 견적이에요. 참여·상품 조건 확인은 새 체험에서 제공해요.</p>');return;}
    $('dialogBody').insertAdjacentHTML('beforeend',`<section id="transferAgreement" class="transfer-agreement"><h3>이 조건으로 바꿀게요</h3><p><b>${esc(spec(t.target_product))}</b></p><p>${esc(t.notice)}</p><p>${esc(t.merchant_rule)}</p><small>${esc(t.confirmation)}</small>${allocation(t.funding,'할인 비용과 매장 수취액 보기')}<small class="agreement-id">확인 조건 ${esc(t.terms_id.slice(0,12))} · 체험용 참여 정책</small></section>`);
  });
  document.addEventListener('route:render',e=>{
    const s=e.detail;
    for(const card of document.querySelectorAll('.route-card')){
      const t=s.all_plans.find(p=>p.store_id===card.dataset.hover)?.transfer_terms;
      if(t)card.insertAdjacentHTML('beforeend',`<p class="agreement-spec">${esc(spec(t.target_product))} · ${t.target_policy?.enabled?'변경 참여 매장':'매장 변경 미참여'}</p>`);
    }
  });
  document.addEventListener('route:aux-render',e=>{
    const s=e.detail,m=s.receipt.settlement;
    const paper=document.querySelector('#receiptContent .receipt-paper');
    if(paper&&m)paper.insertAdjacentHTML('beforeend',`<section id="settlementBreakdown" class="transfer-agreement"><h3>받은 매장에 한 번만 기록했어요</h3><p>${esc(s.order.store_name)} · 모의 수취액 <b>${won(m.merchant_receivable)}</b></p>${allocation(m,'결제·할인 부담 나눠 보기')}</section>`);
  });
})();
