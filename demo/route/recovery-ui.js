/* GET-only progress refresh. The server worker, not this page, drives recovery. */
(() => {
  let timer=null, inFlight=false;
  function schedule(s) {
    clearTimeout(timer);
    if(!s?.handoff_pending || !s.automatic_recovery_enabled)return;
    const recovery=s.handoff?.recovery||{};
    const message=document.getElementById('handoffMessage');
    const review=recovery.state==='REVIEW_REQUIRED';
    if(message)message.textContent=review
      ? '자동 확인을 여러 번 시도했지만 매장과 연결되지 않았어요. 주문은 그대로 보관하고 있으니 다시 확인해 주세요.'
      : '매장의 처리 결과를 자동으로 다시 확인하고 있어요. 새로 주문하거나 버튼을 다시 누르지 않아도 돼요.';
    const banner=document.querySelector('#orderArea .rescue-banner p');
    if(banner)banner.textContent=review
      ? '연결을 확인할 때까지 주문과 혜택을 보관해요. 처리 결과를 다시 확인해 주세요.'
      : '서버가 처리 결과를 자동으로 확인해요. 새 주문을 만들거나 다시 누를 필요 없어요.';
    const button=document.getElementById('recoverHandoff');
    if(button)button.textContent=review?'처리 결과 다시 확인하기':'지금 다시 확인하기';
    timer=setTimeout(refresh,review?5000:1000);
  }
  async function refresh() {
    if(inFlight || !state?.handoff_pending || !state.automatic_recovery_enabled){schedule(state);return;}
    const sid=state.id;
    inFlight=true;
    try {
      const latest=await api('/api/route/journeys/'+sid);
      if(state?.id===sid && latest.version>=state.version){state=latest;render();}
    } catch (_) {
      // Preserve last confirmed data; do not convert a lost GET into success.
    } finally {inFlight=false;schedule(state);}
  }
  document.addEventListener('route:render',e=>schedule(e.detail));
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
})();
