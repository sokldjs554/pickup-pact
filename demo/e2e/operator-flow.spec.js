const { test, expect } = require('@playwright/test');

async function openPage(page, label) {
  await page.getByRole('button', { name: label, exact: true }).click();
}

test('first-time visitor can understand and complete the guided recovery flow', async ({ page }) => {
  await page.goto('/');

  await expect(page.getByRole('heading', { name: '주문은 취소됐는데' })).toBeVisible();
  await expect(page.getByText('기술 용어를 몰라도 아래 안내 순서대로 직접 체험할 수 있습니다.')).toBeVisible();
  await expect(page.locator('#mEvents')).toHaveText('0');
  await expect(page.locator('#mAnomalies')).toHaveText('0');

  await page.getByRole('button', { name: '3분 데모 시작', exact: true }).click();
  await expect(page.locator('#guideTitle')).toHaveText('정상 주문부터 만들어보세요.');
  await expect(page.locator('#guideOrder')).toContainText('아직 주문이 없습니다.');

  await page.getByRole('button', { name: '1. 정상 주문 만들기', exact: true }).click();
  await expect(page.locator('#guideTitle')).toHaveText('정상 주문이 준비됐습니다.');
  await expect(page.locator('#guideOrder')).toContainText('아메리카노 2잔');
  await expect(page.locator('#guideOrder')).toContainText('픽업 확정');
  await expect(page.locator('#guideProblemList')).toContainText('아직 표시할 문제가 없습니다.');

  await page.getByRole('button', { name: '2. 52초 지연 취소 만들기', exact: true }).click();
  await expect(page.locator('#guideTitle')).toHaveText('문제가 발생했습니다.');
  await expect(page.locator('#guideBefore')).toContainText('정산 +9,000원 · 포인트 +90P');
  await expect(page.locator('#guideProblemList')).toContainText('취소 뒤에 점주 정산이 반영되었습니다.');
  await expect(page.locator('#guideProblemList')).toContainText('취소 뒤에 포인트가 지급되었습니다.');

  await page.getByRole('button', { name: '3. 문제 확인하기', exact: true }).click();
  await expect(page.locator('#guideTitle')).toHaveText('복구 계획이 준비됐습니다.');

  await page.getByRole('button', { name: '4. 안전하게 복구하기', exact: true }).click();
  await expect(page.locator('#guideTitle')).toHaveText('복구가 끝났습니다.');
  await expect(page.locator('#guideAfter')).toContainText('정산 0원 · 포인트 0P');
  await expect(page.locator('#guideAfter')).toContainText('감사 이력을 보존했습니다.');
  await expect(page.getByRole('button', { name: '다시 체험하기', exact: true })).toBeVisible();

  await page.locator('#page-guided').getByRole('button', { name: '기술 상세 보기', exact: true }).click();
  await expect(page.locator('#anomalyList')).toContainText('현재 탐지된 정합성 이상이 없습니다.');
  await expect(page.locator('#receivedTimeline')).toContainText('SettlementReversed');
  await expect(page.locator('#receivedTimeline')).toContainText('RewardReversed');

  await openPage(page, '정산 · 감사');
  await expect(page.locator('#ledgerSettlement')).toHaveText('0');
  await expect(page.locator('#ledgerReward')).toHaveText('0');
  await expect(page.locator('#ledgerTable')).toContainText('REVERSE_SETTLEMENT');
  await expect(page.locator('#ledgerTable')).toContainText('REVERSE_REWARD');
});

test('reviewer can still operate the full expert flow end to end', async ({ page }) => {
  await page.goto('/');

  await expect(page.getByRole('heading', { name: '주문은 취소됐는데' })).toBeVisible();
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

  await page.locator('.preset').filter({ hasText: '매장이 처리할 수 있는 주문 수 감소' }).click();
  await openPage(page, '정합성 복구');
  await expect(page.locator('#anomalyList')).toContainText('확정한 픽업 약속을 현재 매장 처리량으로 지킬 수 없습니다.');
  await expect(page.locator('#repairList')).toContainText('대체 픽업 시간 검토');

  await openPage(page, '홈');
  await page.locator('.preset').filter({ hasText: '같은 메시지 번호인데 금액 충돌' }).click();
  await openPage(page, '정합성 복구');
  await expect(page.locator('#anomalyList')).toContainText('같은 이벤트 ID인데 내용이 다릅니다.');
  await expect(page.locator('#repairList')).toContainText('자동 처리 중단');
});

test('guided quick start remains usable on a narrow mobile viewport', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');

  await expect(page.getByRole('heading', { name: '주문은 취소됐는데' })).toBeVisible();
  await page.getByRole('button', { name: '3분 데모 시작', exact: true }).click();
  await expect(page.locator('#guideTitle')).toBeVisible();

  await page.getByRole('button', { name: '1. 정상 주문 만들기', exact: true }).click();
  await expect(page.locator('#guideOrder')).toContainText('픽업 확정');

  await page.getByRole('button', { name: '2. 52초 지연 취소 만들기', exact: true }).click();
  await expect(page.locator('#guideBefore')).toContainText('정산 +9,000원 · 포인트 +90P');
});
