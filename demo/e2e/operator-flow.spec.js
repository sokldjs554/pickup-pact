const { test, expect } = require('@playwright/test');

async function openPage(page, label) {
  await page.getByRole('button', { name: label, exact: true }).click();
}

test('virtual customer can browse, add to cart, order, track, and cancel', async ({ page }) => {
  await page.goto('/');

  await expect(page.getByRole('heading', { name: /오늘 뭐 드실래요/ })).toBeVisible();
  await expect(page.locator('#customerPill')).toHaveText('체험 손님 · 하늘');
  await expect(page.locator('.sidebar')).not.toBeVisible();
  await expect(page.getByRole('button', { name: '정합성 복구', exact: true })).not.toBeVisible();

  await expect(page.getByRole('button', { name: /패스카페 강남역점/ })).toBeVisible();
  await expect(page.locator('#selectedStoreName')).toHaveText('패스카페 강남역점');
  await expect(page.locator('#menuList')).toContainText('아메리카노');
  await expect(page.locator('#menuList')).toContainText('카페라떼');

  await page.getByRole('button', { name: '아메리카노 담기', exact: true }).click();
  await page.getByRole('button', { name: '아메리카노 담기', exact: true }).click();
  await expect(page.locator('#cartCount')).toHaveText('2');
  await expect(page.locator('#cartBarTotal')).toHaveText('9,000원');

  await page.getByRole('button', { name: /장바구니 보기/ }).click();
  await expect(page.getByRole('dialog', { name: '장바구니' })).toBeVisible();
  await expect(page.locator('#cartItems')).toContainText('아메리카노');
  await expect(page.locator('#cartTotal')).toHaveText('9,000원');

  await page.getByRole('button', { name: '아메리카노 수량 줄이기', exact: true }).click();
  await expect(page.locator('#cartTotal')).toHaveText('4,500원');
  await page.getByRole('button', { name: '아메리카노 수량 늘리기', exact: true }).click();
  await expect(page.locator('#cartTotal')).toHaveText('9,000원');

  await page.getByRole('button', { name: '9,000원 주문하기', exact: true }).click();
  await expect(page.getByRole('heading', { name: '내 주문', exact: true })).toBeVisible();
  await expect(page.locator('#customerOrderView')).toContainText('아메리카노 2개');
  await expect(page.locator('#customerOrderView')).toContainText('9,000원');

  await expect(page.locator('#customerOrderView')).toHaveAttribute('data-stage', 'promise', { timeout: 10000 });
  await expect(page.locator('#customerOrderView')).toContainText('픽업 시간이 조금 늦어져요.');
  const promiseCard=page.locator('.pickup-promise-card.warning');
  const originalPickup=await promiseCard.getAttribute('data-original-pickup');
  const suggestedPickup=await promiseCard.getAttribute('data-suggested-pickup');
  expect(originalPickup).toMatch(/^\\d{2}:\\d{2}$/);
  expect(suggestedPickup).toMatch(/^\\d{2}:\\d{2}$/);
  const toMinutes=value => {
    const [h,m]=value.split(':').map(Number);
    return h*60+m;
  };
  expect((toMinutes(suggestedPickup)-toMinutes(originalPickup)+1440)%1440).toBe(5);
  await expect(page.getByRole('button', { name: /괜찮아요$/ })).toBeVisible();

  await page.getByRole('button', { name: /괜찮아요$/ }).click();
  await expect(page.locator('#customerOrderView')).toContainText(suggestedPickup+' 픽업');
  await expect(page.locator('#customerOrderView')).toHaveAttribute('data-stage', 'ready', { timeout: 8000 });
  await expect(page.locator('#customerOrderView')).toContainText('픽업 준비됐어요.');

  await page.getByRole('button', { name: '주문 취소', exact: true }).click();
  await expect(page.getByRole('dialog', { name: '주문 취소 확인' })).toBeVisible();
  await page.getByRole('button', { name: '주문 취소', exact: true }).last().click();

  await expect(page.locator('#customerOrderView')).toContainText('주문이 취소됐어요.', { timeout: 12000 });
  await expect(page.locator('#customerOrderView')).toContainText('9,000원 결제 취소');
  await expect(page.getByRole('button', { name: '다시 주문하기', exact: true })).toBeVisible();

  // Recovery stayed hidden from the customer, but the backend evidence is still inspectable.
  await expect(page.getByRole('button', { name: '정합성 복구', exact: true })).not.toBeVisible();

  await page.goto('/?dev=1');
  await expect(page.locator('#orderEvents')).toContainText('PickupRescheduled');
  await openPage(page, '정산 · 감사');
  await expect(page.locator('#auditList')).toContainText('PICKUP_RESLOT_SUGGESTED');
  await expect(page.locator('#auditList')).toContainText('PICKUP_RESCHEDULE_ACCEPTED');
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
