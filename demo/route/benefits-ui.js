/* Render only server-calculated benefits and paired evidence. */
(() => {
  const breakdown = (p, title = '할인이 이렇게 적용돼요', cancelled = false) => p ? `<section class="benefit-breakdown"><h3>${esc(title)}</h3><div><span>메뉴 금액</span><b>${won(p.gross)}</b></div><div><span>${esc(p.coupon_name || '쿠폰 할인')}</span><b>−${won(p.coupon_discount)}</b></div><div><span>사용 포인트</span><b>−${Number(p.points_used).toLocaleString('ko-KR')}P</b></div><div class="benefit-total"><span>${cancelled ? '취소 전 예정 금액' : '할인 후 결제 금액'}</span><b>${won(p.cash_due)}</b></div>${p.selected_coupon_id && p.coupon_reason !== 'APPLIED' ? `<p class="benefit-warning">${esc(couponMessage(p))}</p>` : ''}${p.points_clipped ? `<p>할인 후 남은 금액까지만 ${p.points_used}P 사용해요.</p>` : ''}</section>` : '';
  const walletHTML = w => `<section class="wallet-panel" id="walletPanel"><div class="eyebrow">내 쿠폰과 포인트</div><div class="wallet-amount"><b id="availablePoints">${Number(w.available_points).toLocaleString('ko-KR')}P</b><span>사용 가능</span></div><p>주문에 적용 중 <strong>${w.held_points}P</strong> · 사용 완료 ${w.spent}P · 적립 ${w.earned}P</p><details><summary>보유 쿠폰 ${w.coupons.filter(c => c.status === 'AVAILABLE' || c.status === 'HELD').length}장 확인</summary>${w.coupons.map(c => `<div class="wallet-coupon"><div><b>${esc(c.name)}</b><small>${c.minimum.toLocaleString('ko-KR')}원 이상 · ${time(c.expires_at)}까지</small></div><span>${({AVAILABLE:'사용 가능',HELD:'주문에 적용 중',USED:'사용 완료',EXPIRED:'만료'})[c.status]}</span></div>`).join('')}</details><p class="wallet-footnote">이 체험에서만 쓰는 포인트예요. 다른 주문과 공유하지 않아요.</p></section>`;

  document.addEventListener('route:render', e => {
    const s = e.detail;
    $('coupon').value = s.intent.coupon_id || '';
    $('points').value = s.intent.points || 0;
    for (const card of document.querySelectorAll('.route-card')) {
      const p = s.all_plans.find(row => row.store_id === card.dataset.hover);
      if (!p) continue;
      card.insertAdjacentHTML('beforeend', `<div class="route-benefits"><span>메뉴 ${won(p.pricing.gross)}</span><span>쿠폰 −${won(p.pricing.coupon_discount)}</span><span>포인트 −${p.pricing.points_used}P</span>${p.pricing.selected_coupon_id && p.pricing.coupon_reason !== 'APPLIED' ? `<small>${esc(couponMessage(p.pricing))}</small>` : ''}</div>`);
    }
    const order = s.order;
    if (!order) return;
    $('lockedIntent').insertAdjacentHTML('beforeend', walletHTML(s.wallet));
    const body = document.querySelector('#orderArea .order-card');
    const pricing = order.pricing || s.receipt.pricing;
    body.insertAdjacentHTML('beforeend', breakdown(pricing, '할인이 이렇게 적용돼요', order.state === 'CANCELLED'));
    if (order.state === 'CANCELLED') body.insertAdjacentHTML('beforeend', '<p class="benefit-restored" id="benefitsRestored">✓ 주문에 적용했던 쿠폰과 포인트를 돌려드렸어요. 쿠폰 만료일은 그대로예요.</p>');
    if (order.state === 'PICKED_UP') body.insertAdjacentHTML('beforeend', `<p class="benefit-restored" id="benefitsEarned">✓ ${s.receipt.points_spent}P 사용 · 최종 결제액 기준 ${s.receipt.points_earned}P 적립 완료</p>`);
    const comparison = s.comparison;
    if (comparison?.stay && comparison.alternatives.length) {
      const alt = [...comparison.alternatives].sort((a,b) => a.arrival_at-b.arrival_at || a.cash_due-b.cash_due)[0];
      body.insertAdjacentHTML('beforeend', `<section class="order-comparison" id="orderComparison"><div class="eyebrow">그대로 기다릴 때와 옮길 때</div><div class="compare-columns"><div><span>지금 매장에서 받기</span><b>${time(comparison.stay.arrival_at)} 도착 예상</b><small>${won(comparison.stay.cash_due)}</small></div><div><span>${esc(alt.name)}로 이동</span><b>${time(alt.arrival_at)} 도착 예상</b><small>${won(alt.cash_due)}</small></div></div><p>${alt.minutes_saved > 0 ? `${alt.minutes_saved}분 일찍 도착 예상` : alt.minutes_saved < 0 ? `${-alt.minutes_saved}분 늦게 도착 예상` : '도착 예상 시간은 같아요'} · 결제 금액 차이 ${alt.cash_delta > 0 ? '+' : ''}${won(alt.cash_delta)}${alt.coupon_loss ? ` · 쿠폰 할인 ${won(alt.coupon_loss)} 줄어듦` : ''}</p><small>지금 상태로 계산한 예상이에요. 이후 대기가 늘면 더 늦어질 수 있어요.</small></section>`);
    }
  });

  document.addEventListener('route:aux-render', e => {
    const s=e.detail, order=s.order;
    if(!order)return;
    const pricing=order.pricing||s.receipt.pricing;
    const paper = document.querySelector('#receiptContent .receipt-paper');
    if (paper) {
      paper.insertAdjacentHTML('beforeend', breakdown(pricing, order.state === 'CANCELLED' ? '취소한 주문의 할인 내역 · 결제 없음' : '할인 내역', order.state === 'CANCELLED'));
      paper.insertAdjacentHTML('beforeend', `<div class="receipt-row"><span>사용한 포인트 / 새로 받은 포인트</span><b>${s.receipt.points_spent}P / ${s.receipt.points_earned}P</b></div>`);
    }
  });

  document.addEventListener('route:transfer-preview', e => {
    const {state: s, plan: p} = e.detail;
    const old = s.order.pricing || s.receipt.pricing;
    const lost = Math.max(0, old.coupon_discount - p.pricing.coupon_discount);
    $('dialogBody').insertAdjacentHTML('beforeend', `${lost ? `<div class="benefit-warning prominent" id="couponLossWarning">매장을 바꾸면 쿠폰 할인 ${won(lost)}을 받지 못해요.<br>아래 결제 금액을 확인하고 결정해 주세요.</div>` : ''}${breakdown(p.pricing, '매장을 바꾼 뒤 결제 금액')}`);
  });
  $('useAllPoints').addEventListener('click', () => { $('points').value = 2000; });

  let experiment = null;
  const percent = (n,total) => total ? (100*n/total).toFixed(1)+'%' : '—';
  $('runExperiment').addEventListener('click', () => run(async () => {
    experiment = await api('/api/route/experiments', {seed:Number($('experimentSeed').value), cases:120});
    const s = experiment.summary;
    const rows = Object.values(s.by_scenario);
    $('experimentResults').innerHTML = `<div class="experiment-metrics"><div><span>그대로 기다렸을 때 · 시간 안에 도착</span><b>${percent(s.stay_on_time,s.total)}</b><small>${s.stay_on_time} / ${s.total}건</small></div><div><span>늦어질 때 매장을 바꾸면 · 시간 안에 도착</span><b>${percent(s.transfer_on_time,s.total)}</b><small>${s.transfer_on_time} / ${s.total}건</small></div><div><span>시간 안에 도착하게 된 경우 / 오히려 늦어진 경우</span><b>${s.wins} / ${s.losses}<small>건</small></b><small>결과가 같은 경우 ${s.ties}건 · 처음부터 주문하지 못한 ${s.no_initial_route}건 포함</small></div></div><p class="evidence-note">매장을 바꾼 경우는 ${s.transfers}건이에요. 처음 주문할 수 있었던 ${s.initially_serviceable}건의 평균 결제 금액 차이는 ${won(s.mean_cash_delta)}, 가장 많이 늘어난 금액은 ${won(s.max_cash_increase)}이에요. 어느 쪽이든 늦거나 주문하지 못한 ${s.both_late_or_unserviceable}건도 포함했어요.</p><div class="experiment-table-wrap"><table class="experiment-table"><thead><tr><th>상황</th><th>기다려서 제시간에 도착</th><th>변경 방식을 써서 제시간에 도착</th><th>나아짐 / 나빠짐</th></tr></thead><tbody>${rows.map(r=>`<tr><td>${esc(({'혼잡 없음':'평소처럼 준비할 때','주문 매장만 혼잡':'주문한 매장에 대기가 늘 때','대체 매장도 혼잡':'다른 매장도 바쁠 때','이미 제조 시작':'이미 커피를 만들기 시작했을 때','쿠폰 상실·예산 제한':'쿠폰이 없어져 결제 한도를 넘을 때','선택 후 예상 밖 지연':'매장을 고른 뒤 더 늦어질 때'})[r.label]||r.label)}</td><td>${r.stay_on_time}/${r.total}</td><td>${r.transfer_on_time}/${r.total}</td><td>${r.wins} / ${r.losses}</td></tr>`).join('')}</tbody></table></div><p class="evidence-note">실제 고객의 이용 결과가 아니라 6가지 가상 상황을 섞어 비교했어요. 선택한 뒤 생기는 지연은 미리 알 수 없게 했고, 같은 매장에는 같은 지연을 적용했어요. 패스오더나 다른 프로젝트보다 낫다는 뜻은 아니에요.</p><details class="raw-cases"><summary>옮길 수 없거나 결과가 나빠진 경우</summary>${experiment.cases.filter(r=>r.decision_reason==='ALREADY_PREPARING'||r.decision_reason==='NO_ELIGIBLE_ALTERNATIVE'||r.stay.on_time&&!r.transfer.on_time).sort((a,b)=>Number(b.stay.on_time&&!b.transfer.on_time)-Number(a.stay.on_time&&!a.transfer.on_time)).slice(0,8).map(r=>`<div><b>사례 ${esc(r.case_id)}</b><span>${({ALREADY_PREPARING:'이미 만들기 시작해서 바꿀 수 없어요',NO_ELIGIBLE_ALTERNATIVE:'시간과 예산에 맞는 다른 매장이 없어요',TRANSFER:'옮긴 매장에 대기가 더 생겨 오히려 늦었어요'})[r.decision_reason] || esc(r.decision_reason)}</span></div>`).join('')}</details><div class="evidence-download"><button class="secondary" id="downloadExperiment">120건 전체 결과 받기 · JSON</button><small>상황 묶음 ${experiment.seed} · 결과 확인값 ${esc(experiment.semantic_sha256.slice(0,16))}…</small></div>`;
    $('downloadExperiment').addEventListener('click', () => {
      const url=URL.createObjectURL(new Blob([JSON.stringify(experiment,null,2)],{type:'application/json'}));
      const link=document.createElement('a');link.href=url;link.download=`pickup-pact-paired-seed-${experiment.seed}.json`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
    });
    notify('120가지 상황을 비교했어요. 잘된 경우와 그렇지 않은 경우를 함께 보세요.');
  }));
})();
