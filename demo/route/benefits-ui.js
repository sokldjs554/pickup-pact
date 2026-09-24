/* Render only server-calculated benefits and paired evidence. */
(() => {
  const breakdown = (p, title = '혜택 적용 내역', cancelled = false) => p ? `<section class="benefit-breakdown"><h3>${esc(title)}</h3><div><span>메뉴 금액</span><b>${won(p.gross)}</b></div><div><span>${esc(p.coupon_name || '쿠폰 할인')}</span><b>−${won(p.coupon_discount)}</b></div><div><span>사용 포인트</span><b>−${Number(p.points_used).toLocaleString('ko-KR')}P</b></div><div class="benefit-total"><span>${cancelled ? '취소 전 결제 예정액' : '최종 모의 결제'}</span><b>${won(p.cash_due)}</b></div>${p.selected_coupon_id && p.coupon_reason !== 'APPLIED' ? `<p class="benefit-warning">${esc(p.coupon_message)}</p>` : ''}${p.points_clipped ? `<p>할인 후 남은 금액까지만 ${p.points_used}P 사용해요.</p>` : ''}</section>` : '';
  const walletHTML = w => `<section class="wallet-panel" id="walletPanel"><div class="eyebrow">이 일정의 체험 지갑</div><div class="wallet-amount"><b id="availablePoints">${Number(w.available_points).toLocaleString('ko-KR')}P</b><span>사용 가능</span></div><p>사용 보류 <strong>${w.held_points}P</strong> · 사용 확정 ${w.spent}P · 적립 ${w.earned}P</p><details><summary>보유 쿠폰 ${w.coupons.filter(c => c.status === 'AVAILABLE' || c.status === 'HELD').length}장 확인</summary>${w.coupons.map(c => `<div class="wallet-coupon"><div><b>${esc(c.name)}</b><small>${c.minimum.toLocaleString('ko-KR')}원 이상 · ${time(c.expires_at)}까지</small></div><span>${({AVAILABLE:'사용 가능',HELD:'사용 보류',USED:'사용 완료',EXPIRED:'만료'})[c.status]}</span></div>`).join('')}</details><p class="wallet-footnote">실제 회원 포인트가 아닌, 이 일정만의 모의 지갑입니다.</p></section>`;

  document.addEventListener('route:render', e => {
    const s = e.detail;
    $('coupon').value = s.intent.coupon_id || '';
    $('points').value = s.intent.points || 0;
    for (const card of document.querySelectorAll('.route-card')) {
      const p = s.all_plans.find(row => row.store_id === card.dataset.hover);
      if (!p) continue;
      card.insertAdjacentHTML('beforeend', `<div class="route-benefits"><span>메뉴 ${won(p.pricing.gross)}</span><span>쿠폰 −${won(p.pricing.coupon_discount)}</span><span>포인트 −${p.pricing.points_used}P</span>${p.pricing.selected_coupon_id && p.pricing.coupon_reason !== 'APPLIED' ? `<small>${esc(p.pricing.coupon_message)}</small>` : ''}</div>`);
    }
    const order = s.order;
    if (!order) return;
    $('lockedIntent').insertAdjacentHTML('beforeend', walletHTML(s.wallet));
    const body = document.querySelector('#orderArea .order-card');
    const pricing = order.pricing || s.receipt.pricing;
    body.insertAdjacentHTML('beforeend', breakdown(pricing, '혜택 적용 내역', order.state === 'CANCELLED'));
    if (order.state === 'CANCELLED') body.insertAdjacentHTML('beforeend', '<p class="benefit-restored" id="benefitsRestored">✓ 보류했던 쿠폰과 포인트를 돌려드렸어요. 만료일은 연장되지 않아요.</p>');
    if (order.state === 'PICKED_UP') body.insertAdjacentHTML('beforeend', `<p class="benefit-restored" id="benefitsEarned">✓ ${s.receipt.points_spent}P 사용 · 최종 결제액 기준 ${s.receipt.points_earned}P 적립 완료</p>`);
    const comparison = s.comparison;
    if (comparison?.stay && comparison.alternatives.length) {
      const alt = [...comparison.alternatives].sort((a,b) => a.arrival_at-b.arrival_at || a.cash_due-b.cash_due)[0];
      body.insertAdjacentHTML('beforeend', `<section class="order-comparison" id="orderComparison"><div class="eyebrow">같은 주문의 현재 예상 비교</div><div class="compare-columns"><div><span>현재 매장 유지</span><b>${time(comparison.stay.arrival_at)} 도착</b><small>${won(comparison.stay.cash_due)}</small></div><div><span>${esc(alt.name)}로 이동</span><b>${time(alt.arrival_at)} 도착</b><small>${won(alt.cash_due)}</small></div></div><p>${alt.minutes_saved > 0 ? `${alt.minutes_saved}분 일찍 도착 예상` : alt.minutes_saved < 0 ? `${-alt.minutes_saved}분 늦게 도착 예상` : '도착 예상 시간은 같아요'} · 결제 차액 ${alt.cash_delta > 0 ? '+' : ''}${won(alt.cash_delta)}${alt.coupon_loss ? ` · 쿠폰 할인 ${won(alt.coupon_loss)} 감소` : ''}</p><small>추가 지연 전의 계산값입니다. 실제 도착 보장은 아니에요.</small></section>`);
    }
  });

  document.addEventListener('route:aux-render', e => {
    const s=e.detail, order=s.order;
    if(!order)return;
    const pricing=order.pricing||s.receipt.pricing;
    const paper = document.querySelector('#receiptContent .receipt-paper');
    if (paper) {
      paper.insertAdjacentHTML('beforeend', breakdown(pricing, order.state === 'CANCELLED' ? '취소 전 혜택 내역 · 결제 없음' : '최종 혜택 내역', order.state === 'CANCELLED'));
      paper.insertAdjacentHTML('beforeend', `<div class="receipt-row"><span>사용 확정 포인트 / 적립 포인트</span><b>${s.receipt.points_spent}P / ${s.receipt.points_earned}P</b></div>`);
    }
  });

  document.addEventListener('route:transfer-preview', e => {
    const {state: s, plan: p} = e.detail;
    const old = s.order.pricing || s.receipt.pricing;
    const lost = Math.max(0, old.coupon_discount - p.pricing.coupon_discount);
    $('dialogBody').insertAdjacentHTML('beforeend', `${lost ? `<div class="benefit-warning prominent" id="couponLossWarning">이동하면 쿠폰 할인 ${won(lost)}이 사라져요.<br>새 결제액을 확인한 후 동의해 주세요.</div>` : ''}${breakdown(p.pricing, '이동 후 혜택과 결제액')}`);
  });
  $('useAllPoints').addEventListener('click', () => { $('points').value = 2000; });

  let experiment = null;
  const percent = (n,total) => total ? (100*n/total).toFixed(1)+'%' : '—';
  $('runExperiment').addEventListener('click', () => run(async () => {
    experiment = await api('/api/route/experiments', {seed:Number($('experimentSeed').value), cases:120});
    const s = experiment.summary;
    const rows = Object.values(s.by_scenario);
    $('experimentResults').innerHTML = `<div class="experiment-metrics"><div><span>원래 매장 유지 · 모형 마감 충족</span><b>${percent(s.stay_on_time,s.total)}</b><small>${s.stay_on_time} / ${s.total}건</small></div><div><span>위험 시 매장 이동 · 모형 마감 충족</span><b>${percent(s.transfer_on_time,s.total)}</b><small>${s.transfer_on_time} / ${s.total}건</small></div><div><span>마감 결과 개선 / 악화</span><b>${s.wins} / ${s.losses}<small>건</small></b><small>동일 결과 ${s.ties}건 · 처음부터 주문 불가 ${s.no_initial_route}건 포함</small></div></div><p class="evidence-note">매장 이동 ${s.transfers}건 · 최초 주문 가능 ${s.initially_serviceable}건 기준 평균 결제 차액 ${won(s.mean_cash_delta)} · 최대 증가 ${won(s.max_cash_increase)}. 양쪽 모두 마감 실패 또는 주문 불가 ${s.both_late_or_unserviceable}건도 제외하지 않았어요.</p><div class="experiment-table-wrap"><table class="experiment-table"><thead><tr><th>같은 입력 상황</th><th>유지 성공</th><th>이동 정책 성공</th><th>개선 / 악화</th></tr></thead><tbody>${rows.map(r=>`<tr><td>${esc(r.label)}</td><td>${r.stay_on_time}/${r.total}</td><td>${r.transfer_on_time}/${r.total}</td><td>${r.wins} / ${r.losses}</td></tr>`).join('')}</tbody></table></div><p class="evidence-note">선택 뒤 추가로 생기는 지연은 정책에 미리 알려주지 않아요. 같은 매장에는 두 방식 모두 같은 지연을 적용합니다. 6개 상황을 의도적으로 섞은 합성 비교이며, 국내 서비스나 지원자 대비 우위를 입증하는 수치가 아닙니다.</p><details class="raw-cases"><summary>악화·이동 불가 사례도 보기</summary>${experiment.cases.filter(r=>r.decision_reason==='ALREADY_PREPARING'||r.decision_reason==='NO_ELIGIBLE_ALTERNATIVE'||r.stay.on_time&&!r.transfer.on_time).sort((a,b)=>Number(b.stay.on_time&&!b.transfer.on_time)-Number(a.stay.on_time&&!a.transfer.on_time)).slice(0,8).map(r=>`<div><b>사례 ${esc(r.case_id)}</b><span>${({ALREADY_PREPARING:'제조가 시작되어 이동 금지',NO_ELIGIBLE_ALTERNATIVE:'조건을 만족하는 대체 매장 없음',TRANSFER:'선택 후 추가 지연으로 마감 결과 악화'})[r.decision_reason] || esc(r.decision_reason)}</span></div>`).join('')}</details><div class="evidence-download"><button class="secondary" id="downloadExperiment">전체 120건 원본 JSON</button><small>시드 ${experiment.seed} · 결과 해시 ${esc(experiment.semantic_sha256.slice(0,16))}…</small></div>`;
    $('downloadExperiment').addEventListener('click', () => {
      const url=URL.createObjectURL(new Blob([JSON.stringify(experiment,null,2)],{type:'application/json'}));
      const link=document.createElement('a');link.href=url;link.download=`pickup-pact-paired-seed-${experiment.seed}.json`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
    });
    notify('실패·악화 사례를 포함한 동일 조건 비교를 완료했어요.');
  }));
})();
