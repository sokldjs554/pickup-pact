'use strict';
const $=id=>document.getElementById(id);
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const won=n=>Number(n).toLocaleString('ko-KR')+'원';
const time=n=>{const total=520+Number(n||0);return String(Math.floor(total/60)).padStart(2,'0')+':'+String(total%60).padStart(2,'0');};
const names={RESERVED:'제조 전',PREPARING:'제조 중',READY:'픽업 가능',PICKED_UP:'수령 완료',CANCELLED:'취소 완료'};

// Presentation labels only. Stored events, API codes and amounts stay unchanged.
function friendlyRouteError(detail, status) {
  const messages = {
    STALE_VERSION:'주문 상태가 바뀌었어요. 새로 확인한 뒤 다시 선택해 주세요.',
    STALE_QUOTE:'매장 상황이나 금액이 달라졌어요. 최신 내용을 확인하고 다시 골라주세요.',
    IDEMPOTENCY_CONFLICT:'이미 보낸 요청과 내용이 달라요. 현재 주문을 확인해 주세요.',
    ALREADY_PREPARING:'이미 커피를 만들기 시작했어요. 매장 변경이나 자동 취소는 할 수 없어요.',
    INFEASIBLE:'시간이나 결제 한도에 맞지 않는 매장이에요. 다른 곳을 골라주세요.',
    UNSAFE_START:'현재 상태로는 도착 시간에 맞추기 어려워요. 주문 변경을 먼저 확인해 주세요.',
    TOO_EARLY:'아직 처리할 시간이 아니에요. 체험 시계를 앞당긴 뒤 다시 눌러주세요.',
    BAD_CODE:'수령 번호가 맞지 않아요. 화면에 나온 6자리 번호를 확인해 주세요.',
    NOT_READY:'아직 커피가 준비되지 않았어요. 매장 화면에서 준비 상태를 확인해 주세요.',
    ORDER_EXISTS:'이미 주문이 있어요. 새로 주문하지 않고 매장을 바꿀 수 있어요.',
    NO_ORDER:'먼저 카페를 골라 주문해 주세요.', SAME_STORE:'지금 주문한 매장이에요. 다른 매장을 골라주세요.',
    INVALID_STATE:'지금 주문 상태에서는 할 수 없는 작업이에요. 상태를 다시 확인해 주세요.',
    INVALID_COMMAND:'지원하지 않는 작업이에요. 화면에서 다시 선택해 주세요.',
    TERMINAL:'이미 끝난 주문이에요. 새 체험으로 시작해 주세요.',
    UNKNOWN_STORE:'매장을 찾지 못했어요. 카페 목록에서 다시 골라주세요.',
    BAD_DELAY:'추가할 대기 시간은 1분 이상으로 입력해 주세요.',
    CLOCK_LIMIT:'체험 시간은 3시간까지만 진행할 수 있어요. 처음부터 다시 시작해 주세요.'
  };
  if (detail && messages[detail.code]) return messages[detail.code];
  if (status===404) return '이전 주문을 찾지 못했어요. 처음부터 다시 시작해 주세요.';
  if (status===403) return '이 화면에서 요청을 보낼 수 없어요. 데모를 다시 열어주세요.';
  if (status===400 || status===415 || status===422) return '입력한 내용을 확인해 주세요. 금액과 포인트는 숫자로 입력해 주세요.';
  const message=typeof detail==='string'?detail:detail?.message;
  return typeof message==='string' && /[가-힣]/.test(message) ? message : '처리하지 못했어요. 주문 상태를 확인하고 잠시 후 다시 눌러주세요.';
}
function routeEventTitle(event) {
  const labels={INTENT_CREATED:'도착 시간과 메뉴를 골랐어요',BENEFITS_HELD:'쿠폰과 포인트를 주문에 적용했어요',
    PAYMENT_AUTHORIZED:'체험 결제를 승인했어요',AUTHORIZATION_ADJUSTED:'바뀐 금액으로 체험 결제를 조정했어요',
    BENEFITS_CONSUMED:'쿠폰·포인트 사용과 적립을 마쳤어요',BENEFITS_RELEASED:'사용하려던 쿠폰과 포인트를 돌려드렸어요',
    ORDER_READY:'커피가 준비됐어요',ORDER_CANCELLED:'주문을 취소하고 결제 승인을 풀었어요',
    PICKUP_COMPLETED:'커피를 받았어요',PAYMENT_CAPTURED:'체험 결제를 완료했어요',ARRIVAL_UPDATED:'출발 시간을 늦췄어요'};
  if (event.type==='PREPARATION_STARTED') return event.title.replace('제조를 시작했어요','커피를 만들기 시작했어요');
  return labels[event.type]||event.title;
}
function routeReason(code,fallback) {
  return ({MENU:'선택한 음료가 없어요',MILK:'오트로 바꿀 수 없어요',DECAF:'디카페인이 없어요',
    BUDGET:'결제 한도를 넘어요',DEADLINE:'도착 시간이 늦어요',DETOUR:'허용한 시간보다 더 돌아가요',
    OFFLINE:'지금은 주문을 받지 않아요'})[code]||fallback;
}
function couponMessage(pricing) {
  return ({NONE:'쿠폰을 선택하지 않았어요.',APPLIED:'쿠폰을 적용했어요.',
    STORE_MISMATCH:'이 매장에서는 선택한 쿠폰을 쓸 수 없어요.',MINIMUM:'쿠폰을 쓰기에는 주문 금액이 부족해요.',
    EXPIRED:'체험 시간을 기준으로 사용 기간이 지난 쿠폰이에요.',USED:'이미 사용한 쿠폰이에요.'})[pricing.coupon_reason]||pricing.coupon_message;
}

let state=null,catalog=null,drink='latte',activeView='customer',pendingPlan=null,busy=false,toastTimer;
function notify(message,error=false){clearTimeout(toastTimer);$('notice').textContent=message;$('notice').className=error?'error':'';$('notice').hidden=false;toastTimer=setTimeout(()=>$('notice').hidden=true,6500);}
async function api(path,body){const r=await fetch(path,{method:body===undefined?'GET':'POST',headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});let data;try{data=await r.json();}catch(_){throw Error('화면을 불러오지 못했어요. 잠시 후 다시 눌러주세요.');}if(!r.ok){const e=Error(friendlyRouteError(data.detail,r.status));e.status=r.status;throw e;}return data;}
async function run(fn){if(busy)return;busy=true;document.body.classList.add('busy');try{await fn();}catch(e){notify(e.message,true);if(state&&(e.status===409||!e.status)){try{state=await api('/api/route/journeys/'+state.id);render();}catch(_){}}}finally{busy=false;document.body.classList.remove('busy');}}
function requestId(){return (globalThis.crypto?.randomUUID?.()||Date.now()+'-'+Math.random().toString(36).slice(2)).replaceAll('-','');}
async function cmd(action,extra={}){
  state=await api('/api/route/journeys/'+state.id+'/commands',{action,expected_version:state.version,request_id:requestId(),...extra});
  render();
  // An HTTP 200 can report a durable pending/rejected operation. Never let
  // the caller show its old unconditional success toast in either case.
  const op=state.handoff;
  if(state.handoff_pending || (op?.status==='REJECTED' && (op.action===action||action==='recover'))){
    throw Error(op?.message||'매장 확인이 아직 끝나지 않았어요. 다시 확인해 주세요.');
  }
  return state;
}
function labelIntent(intent){return (intent.drink==='latte'?'카페라떼':'아메리카노')+(intent.milk==='oat'?' · 오트':'')+(intent.decaf?' · 디카페인':'');}
function switchView(view){activeView=view;for(const v of ['customer','merchant','receipt'])$(v+'View').hidden=v!==view;document.querySelectorAll('.nav').forEach(b=>b.classList.toggle('active',b.dataset.view===view));renderAux();}
function adjustDeadline(){const n=Number($('deadline').value);$('deadlineValue').textContent=n;$('deadlineClock').textContent=time(n)+'까지';}
function pickDrink(value){drink=value;document.querySelectorAll('[data-drink]').forEach(b=>{const on=b.dataset.drink===value;b.classList.toggle('active',on);b.setAttribute('aria-pressed',String(on));});$('oat').disabled=value==='americano';if(value==='americano')$('oat').checked=false;}
function fillIntent(i){$('destination').value=i.destination;$('deadline').value=i.deadline_minutes;$('budget').value=i.budget;$('detour').value=i.max_detour;$('priority').value=i.priority;pickDrink(i.drink);$('oat').checked=i.milk==='oat';$('decaf').checked=i.decaf;adjustDeadline();}
function cafeIcon(id){return `<div class="cafe-icon ${esc(id)}">${({wave:'W',corner:'C',oat:'O',garden:'G',express:'E'})[id]||'P'}</div>`;}
function selectedPlan(){return state?.current_plan||state?.recommendations?.[0]||null;}
function drawMap(plan=selectedPlan()){
 if(!catalog)return;
 const svg=$('map');const coords=id=>{const p=catalog.nodes[id];return [p[0]*9.3+25,p[1]*6.1+15];};
 const rects=[[105,145,100,130],[335,160,105,135],[435,355,155,90],[690,80,80,105],[740,300,130,94],[220,460,140,60],[820,450,90,90],[70,520,90,46],[415,68,150,74],[690,514,90,45]];
 let html=`<defs><pattern id="grid" width="30" height="30" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r=".9" fill="#ced9c4"/></pattern><filter id="shadow"><feDropShadow dx="0" dy="5" stdDeviation="5" flood-color="#19432b" flood-opacity=".08"/></filter></defs><rect width="1000" height="660" fill="#f5f8f0"/><rect width="1000" height="660" fill="url(#grid)" opacity=".5"/><path d="M36 93Q95 28 211 85L234 167Q209 230 62 222Z" fill="#deebce"/><path d="M856 500Q968 407 998 445V648H889Z" fill="#e0ebd3"/>`;
 for(const [x,y,w,h] of rects)html+=`<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="13" fill="#e9eee1" stroke="#e0e8d7"/>`;
 for(const [a,b] of catalog.edges){const x=coords(a),y=coords(b);html+=`<path d="M${x} L${y}" stroke="#e4eadd" stroke-width="26" stroke-linecap="round"/><path d="M${x} L${y}" stroke="#fff" stroke-width="21" stroke-linecap="round"/>`;}
 html+=`<text x="111" y="138" text-anchor="middle" fill="#92ad7b" font-size="15" class="map-text">센트럴 파크</text><text x="461" y="110" fill="#bbc8b0" font-size="13" letter-spacing="3" class="map-text">카페 거리</text><text x="390" y="406" fill="#b0bfa6" font-size="13" letter-spacing="3" class="map-text">출근길</text>`;
 if(plan){const path=plan.route.map((id,i)=>(i?'L':'M')+coords(id).join(',')).join(' ');const color=state?.risk.needs_attention&&state?.order?.store_id===plan.store_id?'#bb7751':'#287c58';html+=`<path d="${path}" fill="none" stroke="${color}" stroke-opacity=".12" stroke-width="25" stroke-linecap="round" stroke-linejoin="round"/><path d="${path}" fill="none" stroke="${color}" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/><path d="${path}" fill="none" stroke="#ecf6e5" stroke-width="2" stroke-dasharray="2 16"/>`;}
 const all=state?.all_plans||[];
 for(const s of catalog.stores){const [x,y]=coords(s.id),p=all.find(p=>p.store_id===s.id),selected=plan?.store_id===s.id;let fill=selected?'#174b38':'#fff',textColor=selected?'#fff':'#4f6f4e';
  const label=p?(p.feasible?(time(p.arrival_at)+' 도착 예상'):routeReason(p.reasons[0],p.reason_labels[0])):s.name;
  html+=`<g transform="translate(${x},${y})" filter="url(#shadow)"><circle r="${selected?24:19}" fill="${fill}" stroke="${selected?'#174b38':'#c9d8bd'}" stroke-width="2"/><text y="6" text-anchor="middle" font-size="17" font-weight="800" fill="${textColor}" class="map-text">${({wave:'W',corner:'C',oat:'O',garden:'G',express:'E'})[s.id]}</text><rect x="-70" y="28" width="140" height="${p?47:30}" rx="9" fill="#ffffffed" stroke="${selected?'#b4cca2':'#e0e8d8'}"/><text y="47" text-anchor="middle" font-size="13" font-weight="700" fill="#234b37" class="map-text">${esc(s.name)}</text>${p?`<text y="65" text-anchor="middle" font-size="10" fill="${p.feasible?'#488252':'#a08571'}" class="map-text">${esc(label)}</text>`:''}</g>`;
 }
 for(const [id,label] of [['station','가상역 · 출발'],[state?.intent.destination||$('destination').value,'목적지']]){const [x,y]=coords(id);html+=`<g transform="translate(${x},${y})"><circle r="26" fill="${id==='station'?'#d7ec99':'#e9b48c'}"/><circle r="9" fill="#204d36"/><text y="-37" text-anchor="middle" font-size="15" font-weight="800" fill="#345438" class="map-text">${label}</text></g>`;}
 svg.innerHTML=html;
 $('mapTitle').textContent=plan?plan.name+'를 거쳐, '+time(plan.arrival_at)+' 도착 예상':'커피를 받고, 목적지까지.';
 if(plan)$('mapSummary').innerHTML=`<div><span>커피 받을 시간</span><b>${time(plan.pickup_at)}</b></div><span class="summary-arrow">→</span><div><span>목적지 도착 예상</span><b>${time(plan.arrival_at)}</b></div><span class="summary-arrow">→</span><div><span>약속 시간까지 ${plan.margin>=0?'여유':'지연'}</span><b>${Math.abs(plan.margin)}분 ${plan.margin>=0?'남아요':'늦어요'}</b></div>`;
}
function routeCard(p,index){const transfer=!!state.order;const diff=transfer?p.price-state.order.price:0;return `<article class="route-card ${index===0?'best':''}" data-hover="${esc(p.store_id)}">${cafeIcon(p.store_id)}<div class="route-copy"><h3>${esc(p.name)}${index===0?'<span class="tag">먼저 살펴보세요</span>':''}</h3><p>${esc(p.subtitle)}</p><div class="route-meta"><strong>${time(p.arrival_at)} 도착 예상</strong><span>걷기 ${p.walk_total}분</span><span>매장 대기 ${p.wait}분</span></div></div><div class="route-right"><div class="route-price">${won(p.price)}${transfer?`<small> / 금액 차이 ${diff>0?'+':''}${won(diff)}</small>`:''}</div><button class="primary" data-quote="${esc(p.quote_id)}">${transfer?'이 매장으로 바꾸기':'여기서 주문하기'} <span>↗</span></button></div><div class="why">약속 시간까지 ${p.margin}분 여유 · 바로 갈 때보다 ${p.detour}분 더 걸어요 · ${esc(labelIntent(state.intent))} 그대로</div></article>`;}
function renderRoutes(){if(!state)return;$('routeArea').hidden=false;const rows=state.recommendations;const active=state.order;
 if(active&&active.state!=='RESERVED'){$('routeArea').hidden=true;return;}
 let html=`<div class="route-heading"><div><h2>${active?'대신 들를 수 있는 카페':'내 일정에 맞는 카페'}</h2><p>${active?'바뀌는 할인과 금액을 먼저 확인하세요. 선택한 음료와 주문 번호는 그대로예요.':'커피가 준비될 때까지의 대기와 목적지까지 걷는 시간을 함께 봤어요.'}</p></div><span>${rows.length}곳 가능</span></div>`;
 if(rows.length)html+='<div class="route-list">'+rows.map(routeCard).join('')+'</div>';
 else html+=`<div class="empty-state"><span>↗</span><h2>지금 조건에 맞는 ${active?'다른 ':''}매장이 없어요.</h2><p>선택한 메뉴와 결제 한도를 지킬 수 있는 곳을 찾지 못했어요.<br>${active?'현재 주문은 그대로예요. 아직 만들기 전이라면 취소할 수 있어요.':'도착 시간이나 결제 한도, 돌아가도 괜찮은 시간을 바꿔 다시 찾아보세요.'}</p></div>`;
 const excluded=state.all_plans.filter(p=>!p.feasible);if(excluded.length)html+=`<details class="excluded"><summary>다른 ${excluded.length}곳을 추천하지 않은 이유</summary>${excluded.map(p=>`<div class="excluded-row"><b>${esc(p.name)}</b><span>${p.reasons.map((r,i)=>esc(routeReason(r,p.reason_labels[i]))).join(' · ')}</span></div>`).join('')}</details>`;
 if(!active&&rows.length){const eligible=state.all_plans.filter(p=>!p.reasons.some(r=>['MENU','MILK','DECAF','OFFLINE'].includes(r)));const nearest=eligible.sort((a,b)=>a.walk_to-b.walk_to)[0];if(nearest&&nearest.store_id!==rows[0].store_id)html+=`<div class="comparison-note">거리만 보면 <b>${esc(nearest.name)}</b>, 도착 시간까지 보면 <b>${esc(rows[0].name)}</b>.<br>목적지 도착 예상 ${time(nearest.arrival_at)} → ${time(rows[0].arrival_at)}<small>가까운 순서로 고른 결과와 비교했어요. 패스오더의 실제 추천 결과와는 달라요.</small></div>`;}
 $('routeArea').innerHTML=html;
}
function orderActions(){const o=state.order;switch(o.state){case 'RESERVED':return `<div class="order-actions"><button class="primary" data-view="merchant">매장 화면 보기 <span>→</span></button><button class="secondary" data-action="cancel">주문 취소</button></div>`;case 'PREPARING':return `<div class="order-actions"><button class="primary" data-view="merchant">준비 상태 확인하기 <span>→</span></button></div>`;case 'READY':return `<div class="pickup"><p>커피를 받을 때 보여줄 번호</p><strong id="pickupCode">${esc(o.pickup_code)}</strong><label for="claimCode" class="muted"><small>위 번호를 아래에 입력하면 수령을 체험할 수 있어요.</small></label><div><input id="claimCode" inputmode="numeric" maxlength="6" placeholder="6자리 번호" aria-label="수령 번호"><button class="primary" id="claim">커피 받았어요</button></div></div>`;default:return `<div class="order-actions"><button class="primary" data-view="receipt">영수증 보기 <span>↗</span></button></div>`;}}
function renderOrder(){const o=state?.order;$('orderArea').hidden=!o;if(!o)return;const p=state.current_plan,transfers=state.receipt.transfers;const title=o.state==='PICKED_UP'?'커피를 받았어요.':o.state==='CANCELLED'?'주문을 취소했어요.':o.state==='READY'?'커피가 준비됐어요.':transfers.length?'매장을 바꿨어요. 주문은 그대로예요.':'주문한 커피를 확인하세요.';
 let html=`<article class="order-card"><div class="order-top"><div><span class="eyebrow">내 주문 / ${esc(o.id)}</span><h2>${title}</h2></div><span class="status-pill">${names[o.state]}</span></div><p class="order-subtitle"><b>${esc(o.store_name)}</b> · ${esc(labelIntent(state.intent))}</p><div class="order-stats"><div><span>목적지 도착 예상</span><b>${time(p.arrival_at)}</b></div><div><span>약속한 도착 시간</span><b>${time(state.intent.deadline_minutes)}</b></div><div><span>체험 주문 금액</span><b>${won(o.price)}</b></div></div>`;
 if(transfers.length){const t=transfers.at(-1);html+=`<div class="success-banner">✓ ${esc(t.data.from_name)}에서 ${esc(t.data.to_name)}로 이동했어요.<br>주문 번호 ${esc(o.id)}는 그대로예요. 다시 주문하지 않아도 돼요.</div>`;}
 if(state.risk.needs_attention){html+=`<div class="rescue-banner"><h3>${p.margin<0?'지금 매장에서는 '+Math.abs(p.margin)+'분 늦을 수 있어요.':'약속한 시간에 맞추기 어려워요.'}</h3><p>${state.risk.can_transfer?'아래에서 다른 매장을 골라보세요. 할인과 금액을 확인한 뒤 바꿀 수 있어요.':'이미 커피를 만들기 시작해서 매장을 바꿀 수 없어요. 현재 매장의 준비 상태를 확인해 주세요.'}</p></div>`;}
 html+=orderActions()+`<p class="order-reference">체험 결제 승인 ${state.receipt.authorization_count}건 · 결제 완료 ${state.receipt.capture_count}건 · 새로고침해도 주문은 남아요</p>`;
 if(!['CANCELLED','PICKED_UP'].includes(o.state))html+=`<details class="scenario-controls" open><summary>매장이 바빠지거나 출발이 늦어진다면?</summary><p>아래 버튼으로 가상 상황을 바꿔볼 수 있어요. 도착 예상과 가능한 매장을 다시 확인해 보세요.</p><div class="order-actions"><button class="secondary" data-action="busy">이 매장에 대기 12분 추가</button><button class="secondary" data-action="delay">출발을 3분 늦추기</button><button class="secondary" data-action="refresh">최신 상태 확인</button></div></details>`;
 $('orderArea').innerHTML=html+'</article>';
}
function emptyView(title){return `<div class="empty-view"><h2>${title}</h2><p>내 주문에서 시간과 메뉴를 먼저 골라주세요.</p><button class="primary" data-view="customer">카페 찾으러 가기</button></div>`;}
function renderMerchant(){if(!state?.order){$('merchantContent').innerHTML=emptyView('아직 받은 주문이 없어요.');return;}const o=state.order,p=state.current_plan;
 const tiles=state.stores.map(s=>`<div class="merchant-tile ${s.id===o.store_id?'selected':''}">${cafeIcon(s.id)}<div><h3>${esc(s.name)}</h3><p>${s.id===o.store_id?(o.state==='CANCELLED'?'취소된 주문':names[o.state]):'이 주문의 준비 매장이 아니에요'}</p></div></div>`).join('');
 let controls='';if(o.state==='RESERVED'){const advance=Math.max(0,p.start_at-state.clock);controls=(advance?`<button class="secondary" data-action="advance" data-minutes="${Math.min(30,advance)}">시계 ${Math.min(30,advance)}분 앞당기기</button>`:'')+`<button class="primary" data-action="start" ${advance||state.risk.needs_attention?'disabled':''}>커피 만들기 시작</button>`;}else if(o.state==='PREPARING'){const left=Math.max(0,o.ready_at-state.clock);controls=(left?`<button class="secondary" data-action="advance" data-minutes="${Math.min(30,left)}">시계 ${Math.min(30,left)}분 앞당기기</button>`:'')+`<button class="primary" data-action="ready" ${left?'disabled':''}>준비 완료</button>`;}else controls='<button class="primary" data-view="customer">내 주문에서 확인하기</button>';
 $('merchantContent').innerHTML=`<div class="merchant-layout"><div class="merchant-stores">${tiles}</div><div class="merchant-ticket"><div class="ticket-number">주문 번호 ${esc(o.id)}</div><div class="order-top"><h2>${esc(o.store_name)}</h2><span class="status-pill">${names[o.state]}</span></div><div class="ticket-drink"><span class="mini-cup ${state.intent.drink==='latte'?'latte':'americano'}"></span><div><h3>${esc(labelIntent(state.intent))}</h3><p>1잔 · ${won(o.price)} · 고른 음료 그대로</p></div></div><div class="order-stats"><div><span>고객의 도착 시간</span><b>${time(state.intent.deadline_minutes)}</b></div><div><span>예상 준비 완료</span><b>${time(o.state==='RESERVED'?p.ready_at:o.ready_at)}</b></div><div><span>체험 시간</span><b>${time(state.clock)}</b></div></div><p class="ticket-status">${o.state==='RESERVED'?'아직 만들기 전이에요. 고객이 다른 매장으로 옮기면 이곳에서는 준비하지 않아요.':o.state==='PREPARING'?'커피를 만들고 있어요. 이제 매장을 바꾸거나 자동으로 취소할 수 없어요.':'고객의 주문 화면에도 같은 상태가 보여요.'}</p>${state.risk.needs_attention?'<div class="rescue-banner"><h3>고객의 도착 시간에 맞추기 어려워요.</h3><p>아직 만들기 전이라면 내 주문에서 다른 매장을 골라보세요.</p></div>':''}<div class="order-actions">${controls}</div><details class="scenario-controls"><summary>시간을 앞당기는 이유</summary><p>실제로 기다리지 않고 주문 과정을 살펴볼 수 있도록 체험 시계를 앞당겨요. 실제 위치나 매장의 준비 상태와 연결되어 있지 않아요.</p></details></div></div>`;
}
function renderReceipt(){if(!state?.order){$('receiptContent').innerHTML=emptyView('아직 영수증이 없어요.');return;}const o=state.order,r=state.receipt;
 const rows=[['현재 매장',o.store_name],['메뉴',labelIntent(state.intent)],['주문 상태',names[o.state]],['첫 매장',o.original_store],['매장 이동',r.transfers.length+'회'],['체험 결제 승인',r.authorization_count+'건'],['체험 결제 완료',r.capture_count+'건']];
 const meaningful=state.events.filter(e=>!['CLOCK_ADVANCED'].includes(e.type));
 $('receiptContent').innerHTML=`<div class="receipt-layout"><article class="receipt-paper"><span class="eyebrow">PICKUP PACT / 체험용 영수증</span><h2>주문 내역을 모았어요.</h2><p class="receipt-id">${esc(o.id)}</p>${rows.map(([a,b])=>`<div class="receipt-row"><span>${a}</span><b>${esc(b)}</b></div>`).join('')}${r.transfers.map(t=>`<div class="receipt-row"><span>매장 변경 금액 차이</span><b>${t.data.difference>0?'+':''}${won(t.data.difference)}</b></div>`).join('')}<div class="receipt-row total"><span>${r.capture_count?'체험 결제 완료':'결제 예정 금액'}</span><b>${won(r.capture_count?r.net_paid:r.payable)}</b></div><p>실제 결제 영수증은 아니에요. 매장을 바꿔도 주문은 하나이고, 체험 결제는 수령할 때 한 번만 완료돼요.</p><div class="order-actions"><a class="secondary" href="/api/route/journeys/${esc(state.id)}/receipt" target="_blank" rel="noopener">주문 기록 보기 ↗</a><button class="secondary" data-action="refresh">최신 상태 확인</button></div></article><section class="panel timeline"><h2>주문이 이렇게 진행됐어요</h2>${meaningful.map(e=>`<div class="event-row"><time>${time(e.at)}</time><div class="event-line"></div><div><b>${esc(routeEventTitle(e))}</b><small>${e.type==='ORDER_TRANSFERRED'?'주문 '+esc(e.data.order_id)+' 유지':e.type==='INTENT_CREATED'?'선택한 시간과 메뉴 · 체험용 기록':e.data.amount!==undefined?won(e.data.amount):'저장된 주문 기록'}</small></div></div>`).join('')}</section></div>`;
}
function renderAux(){renderMerchant();renderReceipt();if(state)document.dispatchEvent(new CustomEvent('route:aux-render',{detail:state}));}
function render(){if(!state)return;localStorage.setItem('pickup-pact.route-journey',state.id);$('clock').textContent=time(state.clock);$('startHint').hidden=true;document.body.classList.toggle('compact',!!state.order);$('intentForm').hidden=!!state.order;$('lockedIntent').hidden=!state.order;
 if(state.order){$('lockedIntent').innerHTML=`<h3 class="locked-title">${time(state.intent.deadline_minutes)}까지, ${state.intent.destination==='office'?'오피스 타워':'센트럴 파크'}.</h3><div class="locked-info"><span><b>${esc(labelIntent(state.intent))}</b></span><span>예산 <b>${won(state.intent.budget)}</b></span><span>돌아가도 괜찮은 시간 <b>${state.intent.max_detour}분</b></span><span>출발 <b>가상역 2번 출구</b></span></div><p class="locked-statement">음료와 주문 번호는 그대로예요.<br><b>할인이 달라지면 금액을 먼저 알려드려요.</b><br>결제 한도 안에서 직접 선택하세요.</p>`;}
 $('stepLabel').textContent=state.order?(state.order.state==='PICKED_UP'?'04 / 커피 받고 영수증 확인':'03 / 주문 상태 확인하기'):'02 / 들를 카페 고르기';$('stepHint').textContent=state.order?'주문부터 수령까지 한 번에 확인하세요.':'도착 예상과 금액을 보고 골라보세요.';renderOrder();renderRoutes();drawMap();renderAux();document.dispatchEvent(new CustomEvent('route:render',{detail:state}));}
function transferDialog(p){pendingPlan=p;const o=state.order,d=p.price-o.price;$('dialogBody').innerHTML=`<div class="dialog-box">${esc(o.store_name)}<br><b>↓ ${esc(p.name)}</b></div><div class="dialog-detail"><span>주문 번호</span><b>${esc(o.id)}</b></div><div class="dialog-detail"><span>선택한 음료</span><b>${esc(labelIntent(state.intent))}</b></div><div class="dialog-detail"><span>체험 주문 금액</span><b>${won(o.price)} → ${won(p.price)}</b></div><div class="dialog-detail"><span>금액 차이</span><b>${d>0?'+':''}${won(d)}</b></div><div class="dialog-detail"><span>목적지 도착 예상</span><b>${time(p.arrival_at)} · ${p.margin}분 여유</b></div><p class="muted" style="font-size:10px;margin-top:14px">확인 버튼을 눌러야 매장이 바뀌어요. 바꾸기 직전에 매장 상태와 결제 한도를 다시 확인해요.</p>`;document.dispatchEvent(new CustomEvent('route:transfer-preview',{detail:{state,plan:p}}));$('confirmDialog').showModal();}
$('intentForm').addEventListener('submit',e=>{e.preventDefault();run(async()=>{state=await api('/api/route/journeys',{destination:$('destination').value,deadline_minutes:Number($('deadline').value),drink,milk:$('oat').checked?'oat':'regular',decaf:$('decaf').checked,budget:Number($('budget').value),max_detour:Number($('detour').value),priority:$('priority').value,coupon_id:$('coupon').value||null,points:Number($('points').value)});render();notify(state.recommendations.length?'들를 수 있는 카페를 찾았어요.':'맞는 매장이 없어요. 시간이나 결제 한도를 바꿔보세요.',!state.recommendations.length);$('routeArea').scrollIntoView({behavior:'smooth',block:'nearest'});});});
$('deadline').addEventListener('input',adjustDeadline);$('destination').addEventListener('change',()=>{if(!state)drawMap();});
$('reset').addEventListener('click',()=>{localStorage.removeItem('pickup-pact.route-journey');location.href='/';});
$('dismissDialog').addEventListener('click',()=>$('confirmDialog').close());
$('confirmTransfer').addEventListener('click',()=>run(async()=>{const p=pendingPlan;$('confirmDialog').close();await cmd('transfer',{quote_id:p.quote_id});notify('주문은 그대로 두고, '+p.name+'로 옮겼어요.');}));
document.addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;if(b.dataset.view){switchView(b.dataset.view);window.scrollTo({top:0,behavior:'smooth'});return;}if(b.dataset.drink){pickDrink(b.dataset.drink);return;}if(b.dataset.quote){const p=state.recommendations.find(p=>p.quote_id===b.dataset.quote);if(!p)return;if(state.order){transferDialog(p);return;}run(async()=>{await cmd('reserve',{quote_id:p.quote_id});notify(p.name+'에 주문했어요. 늦어지면 만들기 전에 매장을 바꿀 수 있어요.');window.scrollTo({top:0,behavior:'smooth'});});return;}if(b.id==='claim'){run(async()=>{await cmd('claim',{pickup_code:$('claimCode').value.trim()});notify('수령했어요. 체험 결제 내역은 영수증에서 확인하세요.');});return;}
 if(b.dataset.action){run(async()=>{switch(b.dataset.action){case 'busy':await cmd('disrupt',{store_id:state.order.store_id,minutes:12});notify('매장에 대기 시간이 생겼어요. 도착 예상과 다른 카페를 확인해 보세요.');break;case 'delay':await cmd('delay',{minutes:3});notify('출발 시간을 늦췄어요. 도착 예상도 확인해 주세요.');break;case 'advance':await cmd('advance',{minutes:Number(b.dataset.minutes)});break;case 'start':await cmd('start');notify('커피를 만들기 시작했어요.');break;case 'ready':await cmd('ready');notify('커피가 준비됐어요. 내 주문에서 수령 번호를 확인하세요.');break;case 'cancel':await cmd('cancel');notify('만들기 전에 취소했어요. 사용하려던 혜택도 돌려드렸어요.');break;case 'refresh':state=await api('/api/route/journeys/'+state.id);render();notify('최신 주문 상태를 확인했어요.');break;}});}
});
document.addEventListener('mouseover',e=>{const card=e.target.closest('[data-hover]');if(card&&!state?.order){const p=state.all_plans.find(p=>p.store_id===card.dataset.hover);if(p)drawMap(p);}});
(async()=>{try{catalog=await api('/api/route/catalog');drawMap();const saved=localStorage.getItem('pickup-pact.route-journey');if(saved){try{state=await api('/api/route/journeys/'+saved);fillIntent(state.intent);render();}catch(e){localStorage.removeItem('pickup-pact.route-journey');notify('이전 주문을 찾지 못했어요. 처음부터 다시 시작해 주세요.');}}renderAux();}catch(e){notify('화면을 불러오지 못했어요. 잠시 후 새로고침해 주세요.',true);}})();
