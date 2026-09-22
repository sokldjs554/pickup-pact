const { test, expect } = require('@playwright/test');

async function openPage(page, label) {
  await page.getByRole('button', { name: label, exact: true }).click();
}

test('virtual customer completes protected pickup with one-time code and trust receipt', async ({ page }) => {
  await page.goto('/');

  await expect(page.getByRole('heading', { name: /오늘 뭐 드실래요/ })).toBeVisible();
  await expect(page.locator('#customerPill')).toHaveText('체험 손님 · 하늘');
  await expect(page.locator('.sidebar')).not.toBeVisible();

  await page.getByRole('button', { name: '아메리카노 담기', exact: true }).click();
  await page.getByRole('button', { name: '아메리카노 담기', exact: true }).click();
  await expect(page.locator('#cartBarTotal')).toHaveText('9,000원');
  await page.getByRole('button', { name: /장바구니 보기/ }).click();
  await page.getByRole('button', { name: '9,000원 주문하기', exact: true }).click();

  await expect(page.locator('#customerOrderView')).toHaveAttribute('data-stage', 'promise', { timeout: 10000 });
  const promiseCard=page.locator('.pickup-promise-card.warning');
  const originalPickup=await promiseCard.getAttribute('data-original-pickup');
  const suggestedPickup=await promiseCard.getAttribute('data-suggested-pickup');
  const toMinutes=value => value.split(':').map(Number).reduce((h,m)=>h*60+m);
  expect((toMinutes(suggestedPickup)-toMinutes(originalPickup)+1440)%1440).toBe(5);

  await page.getByRole('button', { name: /괜찮아요$/ }).click();
  await expect(page.locator('#customerOrderView')).toHaveAttribute('data-stage', 'ready', { timeout: 8000 });

  const pickupCode=page.locator('.pickup-code-card b');
  await expect(pickupCode).toHaveText(/^\d{4}$/);
  const code=await pickupCode.textContent();
  await page.getByRole('button', { name: '픽업 완료', exact: true }).click();

  await expect(page.locator('#customerOrderView')).toHaveAttribute('data-stage', 'picked');
  await expect(page.locator('#customerOrderView')).toContainText('픽업 완료됐어요.');
  await expect(page.locator('#customerOrderView')).toContainText('사용 완료');

  await page.getByRole('button', { name: '영수증 보기', exact: true }).click();
  const receipt=page.getByRole('dialog', { name: '모바일 영수증' });
  await expect(receipt).toContainText('TRUST RECEIPT');
  await expect(receipt).toContainText('최종 결제');
  await expect(receipt).toContainText('9,000원');
  await expect(receipt).toContainText('픽업 시간 변경');
  await expect(receipt).toContainText('픽업 완료');
  await page.getByRole('button', { name: '영수증 닫기', exact: true }).click();

  await page.getByRole('button', { name: '주문 내역', exact: true }).click();
  await expect(page.locator('#historyList')).toContainText('픽업 완료');
  await expect(page.locator('#historyList')).toContainText('아메리카노 2개');
  await page.locator('#historyList .history-card').first().click();
  await expect(page.getByRole('dialog', { name: '모바일 영수증' })).toContainText('TRUST RECEIPT');
  await page.getByRole('button', { name: '영수증 닫기', exact: true }).click();

  // One-time handoff evidence remains hidden from the customer route.
  await page.goto('/?dev=1');
  await expect(page.locator('#orderEvents')).toContainText('PickupRescheduled');
  await expect(page.locator('#orderEvents')).toContainText('PickupClaimed');
  await openPage(page, '정산 · 감사');
  await expect(page.locator('#auditList')).toContainText('PICKUP_RESLOT_SUGGESTED');
  await expect(page.locator('#auditList')).toContainText('PICKUP_RESCHEDULE_ACCEPTED');
  await expect(page.locator('#auditList')).toContainText('PICKUP_CLAIMED');

  // The consumed pickup code cannot be used a second time.
  const repeat=await page.request.post('/api/demo/sessions/'+await page.evaluate(()=>localStorage.getItem("pickupPactProductSessionV3"))+'/pickup/claim',{data:{code}});
  expect(repeat.status()).toBe(409);
});

test('cart follows the selected store and clears when the customer changes stores', async ({ page }) => {
  await page.goto('/');

  await page.getByRole('button', { name: '카페라떼 담기', exact: true }).click();
  await expect(page.locator('#cartCount')).toHaveText('1');
  await expect(page.locator('#cartBarTotal')).toHaveText('5,000원');

  await page.getByRole('button', { name: /모닝빈 선릉점/ }).click();
  await expect(page.locator('#selectedStoreName')).toHaveText('모닝빈 선릉점');
  await expect(page.locator('#cartBar')).not.toHaveClass(/show/);
  await expect(page.locator('#menuList')).toContainText('햄치즈 샌드위치');

  await page.getByRole('button', { name: '햄치즈 샌드위치 담기', exact: true }).click();
  await expect(page.locator('#cartBarTotal')).toHaveText('6,800원');
  await expect(page.locator('#cartStoreLabel')).toHaveText('모닝빈 선릉점');
});

test('backend reviewer can enter through the hidden dev URL and operate the expert flow', async ({ page }) => {
  await page.goto('/?dev=1');

  await expect(page.locator('.sidebar')).toBeVisible();
  for (const label of ['주문 흐름', '매장 처리량', '장애 주입', '정합성 복구', '정산 · 감사']) {
    await expect(page.getByRole('button', { name: label, exact: true })).toBeVisible();
  }

  await page.getByRole('button', { name: '빈 세션으로 초기화', exact: true }).click();
  await page.getByRole('button', { name: '주문 HOLD 생성', exact: true }).click();
  await expect(page.locator('#orderDetail')).toContainText('HELD');

  await page.getByRole('button', { name: '결제 승인', exact: true }).click();
  await page.getByRole('button', { name: '픽업 확정', exact: true }).click();
  await expect(page.locator('#orderDetail')).toContainText('CONFIRMED');

  await openPage(page, '장애 주입');
  await page.getByRole('button', { name: '52초 지연 취소 + 정산 + 적립', exact: true }).click();
  await expect(page.locator('#faultEvents')).toContainText('SettlementPosted');

  await openPage(page, '정합성 복구');
  await page.getByRole('button', { name: '정합성 다시 계산', exact: true }).click();
  await expect(page.locator('#anomalyList')).toContainText('취소 뒤에 점주 정산이 반영되었습니다.');
  await page.getByRole('button', { name: '안전한 복구 계획을 샌드박스에 적용', exact: true }).click();

  await openPage(page, '정산 · 감사');
  await expect(page.locator('#ledgerSettlement')).toHaveText('0');
  await expect(page.locator('#ledgerReward')).toHaveText('0');
  await expect(page.locator('#ledgerTable')).toContainText('REVERSE_SETTLEMENT');
  await expect(page.locator('#ledgerTable')).toContainText('REVERSE_REWARD');

  await page.getByRole('button', { name: /제품 화면으로 돌아가기/ }).click();
  await expect(page.locator('.sidebar')).not.toBeVisible();
  await expect(page.getByRole('heading', { name: /오늘 뭐 드실래요/ })).toBeVisible();
});

test('customer cancellation recovery remains inspectable only in dev mode', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: '아메리카노 담기', exact: true }).click();
  await page.getByRole('button', { name: '아메리카노 담기', exact: true }).click();
  await page.getByRole('button', { name: /장바구니 보기/ }).click();
  await page.getByRole('button', { name: '9,000원 주문하기', exact: true }).click();

  await page.getByRole('button', { name: '주문 취소', exact: true }).click();
  await page.getByRole('button', { name: '주문 취소', exact: true }).last().click();
  await expect(page.locator('#customerOrderView')).toContainText('주문이 취소됐어요.', { timeout: 12000 });
  await page.getByRole('button', { name: '영수증 보기', exact: true }).click();
  const cancelledReceipt=page.getByRole('dialog', { name: '모바일 영수증' });
  await expect(cancelledReceipt).toContainText('취소 완료');
  await expect(cancelledReceipt).toContainText('최종 결제');
  await expect(cancelledReceipt).toContainText('0원');
  await expect(cancelledReceipt).toContainText('취소 금액');
  await expect(cancelledReceipt).toContainText('-9,000원');
  await expect(cancelledReceipt).toContainText('포인트 조정');
  await page.getByRole('button', { name: '영수증 닫기', exact: true }).click();

  await page.getByRole('button', { name: '주문 내역', exact: true }).click();
  await expect(page.locator('#historyList')).toContainText('취소 완료');

  await page.goto('/?dev=1');
  await openPage(page, '정산 · 감사');
  await expect(page.locator('#ledgerSettlement')).toHaveText('0');
  await expect(page.locator('#ledgerReward')).toHaveText('0');
  await expect(page.locator('#ledgerTable')).toContainText('REVERSE_SETTLEMENT');
  await expect(page.locator('#ledgerTable')).toContainText('REVERSE_REWARD');
  await expect(page.locator('#auditList')).toContainText('REPAIR_PLAN_APPLIED_IN_SANDBOX');
});

test('first customer action waits for a slow session bootstrap', async ({ page }) => {
  let delayed = false;
  await page.route('**/api/demo/sessions', async route => {
    if (!delayed && route.request().method() === 'POST') {
      delayed = true;
      await new Promise(resolve => setTimeout(resolve, 1500));
    }
    await route.continue();
  });

  await page.goto('/');
  await page.getByRole('button', { name: '아메리카노 담기', exact: true }).click();
  await page.getByRole('button', { name: /장바구니 보기/ }).click();
  await page.getByRole('button', { name: '4,500원 주문하기', exact: true }).click();

  await expect(page.locator('#customerOrderView')).toHaveAttribute('data-stage', 'ready', { timeout: 20000 });
  await expect(page.locator('#customerOrderView')).toContainText('4,500원');
  await expect(page.locator('body')).toHaveAttribute('aria-busy', 'false');
});

test('customer smart-order flow remains usable on a narrow mobile viewport', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');

  await expect(page.getByRole('heading', { name: /오늘 뭐 드실래요/ })).toBeVisible();
  await expect(page.locator('#menuList')).toContainText('아메리카노');

  const initialOverflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(initialOverflow).toBeLessThanOrEqual(1);

  await page.getByRole('button', { name: '아메리카노 담기', exact: true }).click();
  await expect(page.locator('#cartBar')).toHaveClass(/show/);
  await page.getByRole('button', { name: /장바구니 보기/ }).click();
  await expect(page.getByRole('dialog', { name: '장바구니' })).toBeVisible();

  const cartOverflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(cartOverflow).toBeLessThanOrEqual(1);

  await page.getByRole('button', { name: '4,500원 주문하기', exact: true }).click();
  await expect(page.getByRole('heading', { name: '내 주문', exact: true })).toBeVisible();

  const orderOverflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(orderOverflow).toBeLessThanOrEqual(1);
});
