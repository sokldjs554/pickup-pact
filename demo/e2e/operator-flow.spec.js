const { test, expect } = require('@playwright/test');

async function openPage(page, label) {
  await page.getByRole('button', { name: label, exact: true }).click();
}

test('reviewer can operate order, inject a late-cancel incident, reconcile, and inspect reversal ledger', async ({ page }) => {
  await page.goto('/');

  await expect(page.getByRole('heading', { name: '주문이 들어오는 것보다,' })).toBeVisible();
  for (const label of ['주문 흐름', '매장 처리량', '장애 주입', '정합성 복구', '정산 · 감사']) {
    await expect(page.getByRole('button', { name: label, exact: true })).toBeVisible();
  }

  await openPage(page, '주문 흐름');
  await page.getByRole('button', { name: '빈 세션으로 초기화', exact: true }).click();
  await expect(page.locator('#orderDetail').getByText('주문이 없습니다. 주문 흐름에서 새 주문을 만들어보세요.')).toBeVisible();

  await page.getByRole('button', { name: '주문 HOLD 생성', exact: true }).click();
  await expect(page.locator('#orderDetail')).toContainText('HELD');
  await expect(page.locator('#orderDetail')).toContainText('UNPAID');

  await page.getByRole('button', { name: '결제 승인', exact: true }).click();
  await expect(page.locator('#orderDetail')).toContainText('PAYMENT AUTH');

  await page.getByRole('button', { name: '픽업 확정', exact: true }).click();
  await expect(page.locator('#orderDetail')).toContainText('CONFIRMED');

  await openPage(page, '장애 주입');
  await page.getByRole('button', { name: '52초 지연 취소 + 정산 + 적립', exact: true }).click();
  await expect(page.locator('#faultEvents')).toContainText('CommitmentCancelled');
  await expect(page.locator('#faultEvents')).toContainText('SettlementPosted');
  await expect(page.locator('#faultEvents')).toContainText('RewardGranted');

  await openPage(page, '정합성 복구');
  await page.getByRole('button', { name: '정합성 다시 계산', exact: true }).click();
  await expect(page.locator('#anomalyList')).toContainText('취소 뒤에 점주 정산이 반영되었습니다.');
  await expect(page.locator('#repairList')).toContainText('점주 정산 보상 분개');
  await expect(page.locator('#repairList')).toContainText('포인트 회수');

  await page.getByRole('button', { name: '안전한 복구 계획을 샌드박스에 적용', exact: true }).click();

  await openPage(page, '정산 · 감사');
  await expect(page.locator('#ledgerSettlement')).toHaveText('0');
  await expect(page.locator('#ledgerReward')).toHaveText('0');
  await expect(page.locator('#ledgerTable')).toContainText('REVERSE_SETTLEMENT');
  await expect(page.locator('#ledgerTable')).toContainText('REVERSE_REWARD');
  await expect(page.locator('#auditList')).toContainText('REPAIR_PLAN_APPLIED_IN_SANDBOX');
});

test('capacity and conflict labs expose distinct operator outcomes', async ({ page }) => {
  await page.goto('/');

  await page.locator('.preset').filter({ hasText: '매장 처리량 4 → 2' }).click();
  await openPage(page, '정합성 복구');
  await expect(page.locator('#anomalyList')).toContainText('확정한 픽업 약속을 현재 매장 처리량으로 지킬 수 없습니다.');
  await expect(page.locator('#repairList')).toContainText('대체 픽업 시간 검토');

  await openPage(page, '개요');
  await page.locator('.preset').filter({ hasText: '같은 event_id, 다른 금액' }).click();
  await openPage(page, '정합성 복구');
  await expect(page.locator('#anomalyList')).toContainText('같은 이벤트 ID인데 내용이 다릅니다.');
  await expect(page.locator('#repairList')).toContainText('자동 처리 중단');
});
