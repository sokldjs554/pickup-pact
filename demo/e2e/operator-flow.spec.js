const { test, expect } = require('@playwright/test');

async function openPage(page, label) {
  await page.getByRole('button', { name: label, exact: true }).click();
}

async function enterExpertMode(page) {
  await page.locator('#page-overview').getByRole('button', { name: '개발자 구현 보기', exact: true }).click();
}

test('first-time visitor gets a product recovery center instead of an operator console', async ({ page }) => {
  await page.goto('/');

  await expect(page.getByRole('heading', { name: /취소된 주문의.*잘못된 정산을 되돌립니다/ })).toBeVisible();
  await expect(page.getByText('주문 보호 기능이 켜져 있습니다.', { exact: true })).toBeVisible();
  await expect(page.getByRole('navigation', { name: '제품 메뉴' })).toBeVisible();
  await expect(page.locator('.protection-preview')).toContainText('아메리카노 2잔');
  await expect(page.locator('.sidebar')).not.toBeVisible();

  for (const label of ['주문 흐름', '매장 처리량', '장애 주입', '정합성 복구', '정산 · 감사']) {
    await expect(page.getByRole('button', { name: label, exact: true })).not.toBeVisible();
  }

  await page.getByRole('button', { name: '복구 시나리오 체험하기', exact: true }).click();

  await expect(page.locator('#guideTitle')).toHaveText('잘못된 처리를 찾았습니다.', { timeout: 15000 });
  await expect(page.locator('#recoveryBanner')).toHaveClass(/problem/);
  await expect(page.locator('#recoveryBannerTitle')).toHaveText('주의가 필요한 주문을 찾았습니다.');
  await expect(page.locator('#guideOrder')).toContainText('아메리카노 2잔');
  await expect(page.locator('#guideOrder')).toContainText('주문 취소');
  await expect(page.locator('#guideBefore')).toContainText('정산 9,000원 · 포인트 90P');
  await expect(page.locator('#guideProblemList')).toContainText('취소 뒤에 점주 정산이 반영되었습니다.');
  await expect(page.locator('#guideProblemList')).toContainText('취소 뒤에 포인트가 지급되었습니다.');
  await expect(page.getByRole('button', { name: '잘못된 처리 되돌리기', exact: true })).toBeVisible();

  await page.getByRole('button', { name: '잘못된 처리 되돌리기', exact: true }).click();

  await expect(page.locator('#guideTitle')).toHaveText('주문 상태를 정상화했습니다.');
  await expect(page.locator('#recoveryBanner')).toHaveClass(/success/);
  await expect(page.locator('#recoveryBannerTitle')).toHaveText('복구가 완료됐습니다.');
  await expect(page.locator('#guideAfter')).toContainText('정산 0원 · 포인트 0P');
  await expect(page.locator('#guideAfter')).toContainText('변경 이력을 보존했습니다.');
  await expect(page.locator('#guideProblemList')).toContainText('해결 완료');

  await page.getByRole('button', { name: '개발자 화면 열기', exact: true }).click();
  await expect(page.locator('.sidebar')).toBeVisible();
  await expect(page.getByRole('button', { name: '제품 화면으로 돌아가기', exact: false })).toBeVisible();
  await expect(page.locator('#anomalyList')).toContainText('현재 탐지된 정합성 이상이 없습니다.');
  await expect(page.locator('#receivedTimeline')).toContainText('SettlementReversed');
  await expect(page.locator('#receivedTimeline')).toContainText('RewardReversed');

  await openPage(page, '정산 · 감사');
  await expect(page.locator('#ledgerSettlement')).toHaveText('0');
  await expect(page.locator('#ledgerReward')).toHaveText('0');
  await expect(page.locator('#ledgerTable')).toContainText('REVERSE_SETTLEMENT');
  await expect(page.locator('#ledgerTable')).toContainText('REVERSE_REWARD');

  await page.getByRole('button', { name: /제품 화면으로 돌아가기/ }).click();
  await expect(page.locator('.sidebar')).not.toBeVisible();
  await expect(page.getByRole('heading', { name: /취소된 주문의.*잘못된 정산을 되돌립니다/ })).toBeVisible();
});

test('reviewer can opt into and operate the full expert flow end to end', async ({ page }) => {
  await page.goto('/');
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

test('product recovery start waits for a slow session bootstrap instead of racing a null session', async ({ page }) => {
  let delayed = false;
  await page.route('**/api/demo/sessions', async route => {
    if (!delayed && route.request().method() === 'POST') {
      delayed = true;
      await new Promise(resolve => setTimeout(resolve, 1500));
    }
    await route.continue();
  });

  await page.goto('/');
  await page.getByRole('button', { name: '복구 시나리오 체험하기', exact: true }).click();

  await expect(page.locator('#guideTitle')).toHaveText('잘못된 처리를 찾았습니다.', { timeout: 17000 });
  await expect(page.locator('#guideOrder')).toContainText('아메리카노 2잔');
  await expect(page.locator('#guideBefore')).toContainText('정산 9,000원 · 포인트 90P');
  await expect(page.locator('body')).toHaveAttribute('aria-busy', 'false');
});

test('product shell and recovery flow remain usable on a narrow mobile viewport', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');

  await expect(page.getByRole('heading', { name: /취소된 주문의.*잘못된 정산을 되돌립니다/ })).toBeVisible();
  await expect(page.locator('.protection-preview')).toBeVisible();

  const initialOverflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(initialOverflow).toBeLessThanOrEqual(1);

  await page.getByRole('button', { name: '복구 시나리오 체험하기', exact: true }).click();

  await expect(page.locator('#guideTitle')).toHaveText('잘못된 처리를 찾았습니다.', { timeout: 15000 });
  await expect(page.locator('#guideOrder')).toContainText('주문 취소');
  await expect(page.locator('#guideBefore')).toContainText('정산 9,000원 · 포인트 90P');
  await expect(page.getByRole('button', { name: '잘못된 처리 되돌리기', exact: true })).toBeVisible();

  const recoveryOverflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(recoveryOverflow).toBeLessThanOrEqual(1);
});
