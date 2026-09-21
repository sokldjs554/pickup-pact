const { test, expect } = require('@playwright/test');

async function openPage(page, label) {
  await page.getByRole('button', { name: label, exact: true }).click();
}

async function enterExpertMode(page) {
  await page.locator('#page-overview').getByRole('button', { name: '백엔드 구현 보기', exact: true }).first().click();
}

test('first-time visitor can understand and complete the recruiter recovery story', async ({ page }) => {
  await page.goto('/');

  await expect(page.getByRole('heading', { name: /주문 취소가 늦게 전달되면/ })).toBeVisible();
  await expect(page.getByText('버튼 한 번으로 시작', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '주문 흐름', exact: true })).not.toBeVisible();

  await page.getByRole('button', { name: '직접 확인해보기', exact: true }).click();

  await expect(page.locator('#guideTitle')).toHaveText('문제를 발견했습니다.', { timeout: 15000 });
  await expect(page.locator('#guideOrder')).toContainText('아메리카노 2잔');
  await expect(page.locator('#guideOrder')).toContainText('주문 취소');
  await expect(page.locator('#guideBefore')).toContainText('정산 9,000원 · 포인트 90P');
  await expect(page.locator('#guideProblemList')).toContainText('취소 뒤에 점주 정산이 반영되었습니다.');
  await expect(page.locator('#guideProblemList')).toContainText('취소 뒤에 포인트가 지급되었습니다.');
  await expect(page.getByRole('button', { name: '문제 복구하기', exact: true })).toBeVisible();

  await page.getByRole('button', { name: '문제 복구하기', exact: true }).click();

  await expect(page.locator('#guideTitle')).toHaveText('복구가 끝났습니다.');
  await expect(page.locator('#guideAfter')).toContainText('정산 0원 · 포인트 0P');
  await expect(page.locator('#guideAfter')).toContainText('변경 이력을 보존했습니다.');
  await expect(page.locator('#guideProblemList')).toContainText('해결 완료');
  await expect(page.getByRole('button', { name: '다시 체험하기', exact: true })).toBeVisible();

  await page.locator('#page-guided').getByRole('button', { name: '백엔드 구현 보기', exact: true }).click();
  await expect(page.getByRole('button', { name: '주문 흐름', exact: true })).toBeVisible();
  await expect(page.locator('#anomalyList')).toContainText('현재 탐지된 정합성 이상이 없습니다.');
  await expect(page.locator('#receivedTimeline')).toContainText('SettlementReversed');
  await expect(page.locator('#receivedTimeline')).toContainText('RewardReversed');

  await openPage(page, '정산 · 감사');
  await expect(page.locator('#ledgerSettlement')).toHaveText('0');
  await expect(page.locator('#ledgerReward')).toHaveText('0');
  await expect(page.locator('#ledgerTable')).toContainText('REVERSE_SETTLEMENT');
  await expect(page.locator('#ledgerTable')).toContainText('REVERSE_REWARD');
});

test('reviewer can opt into and operate the full expert flow end to end', async ({ page }) => {
  await page.goto('/');

  for (const label of ['주문 흐름', '매장 처리량', '장애 주입', '정합성 복구', '정산 · 감사']) {
    await expect(page.getByRole('button', { name: label, exact: true })).not.toBeVisible();
  }

  await enterExpertMode(page);

  for (const label of ['주문 흐름', '매장 처리량', '장애 주입', '정합성 복구', '정산 · 감사']) {
    await expect(page.getByRole('button', { name: label, exact: true })).toBeVisible();
  }

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

test('capacity and conflict labs still expose distinct expert outcomes', async ({ page }) => {
  await page.goto('/');
  await enterExpertMode(page);

  await openPage(page, '매장 처리량');
  await page.getByRole('button', { name: '완성된 장애 예시 불러오기', exact: true }).click();
  await openPage(page, '정합성 복구');
  await expect(page.locator('#anomalyList')).toContainText('확정한 픽업 약속을 현재 매장 처리량으로 지킬 수 없습니다.');
  await expect(page.locator('#repairList')).toContainText('대체 픽업 시간 검토');

  await openPage(page, '주문 흐름');
  await page.getByRole('button', { name: '빈 세션으로 초기화', exact: true }).click();
  await page.getByRole('button', { name: '주문 HOLD 생성', exact: true }).click();
  await page.getByRole('button', { name: '결제 승인', exact: true }).click();
  await page.getByRole('button', { name: '픽업 확정', exact: true }).click();
  await page.getByRole('button', { name: '정상 정산 반영', exact: true }).click();

  await openPage(page, '장애 주입');
  await page.getByRole('button', { name: '금액 충돌 주입', exact: true }).click();
  await openPage(page, '정합성 복구');
  await page.getByRole('button', { name: '정합성 다시 계산', exact: true }).click();
  await expect(page.locator('#anomalyList')).toContainText('같은 이벤트 ID인데 내용이 다릅니다.');
  await expect(page.locator('#repairList')).toContainText('자동 처리 중단');
});

test('one-click recruiter demo waits for a slow session bootstrap instead of racing a null session', async ({ page }) => {
  let delayed = false;
  await page.route('**/api/demo/sessions', async route => {
    if (!delayed && route.request().method() === 'POST') {
      delayed = true;
      await new Promise(resolve => setTimeout(resolve, 1500));
    }
    await route.continue();
  });

  await page.goto('/');
  await page.getByRole('button', { name: '직접 확인해보기', exact: true }).click();

  await expect(page.locator('#guideTitle')).toHaveText('문제를 발견했습니다.', { timeout: 17000 });
  await expect(page.locator('#guideOrder')).toContainText('아메리카노 2잔');
  await expect(page.locator('#guideBefore')).toContainText('정산 9,000원 · 포인트 90P');
  await expect(page.locator('body')).toHaveAttribute('aria-busy', 'false');
});

test('one-click recruiter story remains usable on a narrow mobile viewport', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');

  await expect(page.getByRole('heading', { name: /주문 취소가 늦게 전달되면/ })).toBeVisible();
  await page.getByRole('button', { name: '직접 확인해보기', exact: true }).click();

  await expect(page.locator('#guideTitle')).toHaveText('문제를 발견했습니다.', { timeout: 15000 });
  await expect(page.locator('#guideOrder')).toContainText('주문 취소');
  await expect(page.locator('#guideBefore')).toContainText('정산 9,000원 · 포인트 90P');
  await expect(page.getByRole('button', { name: '문제 복구하기', exact: true })).toBeVisible();
});
