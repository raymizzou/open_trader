import { expect, test, type Page } from '@playwright/test';

async function openPrediction(page: Page, state = 'ready') {
  await page.goto(`/?prediction_state=${state}`, { waitUntil: 'networkidle' });
  await page.getByRole('button', { name: '预测市场', exact: true }).click();
  await expect(page.locator('#prediction-market-workspace')).toBeVisible();
  await expect(page.getByRole('heading', { name: '预测套利 · 机会' })).toBeVisible();
}

test.describe('unified N_LEG opportunity page', () => {
  test('renders the approved mock blocks in order on desktop and mobile', async ({ page }) => {
    for (const viewport of [{ width: 1440, height: 1100 }, { width: 375, height: 812 }]) {
      await page.setViewportSize(viewport);
      await openPrediction(page);
      const root = page.locator('#prediction-market-workspace');
      const markers = ['pm-mode-bar', 'pm-venue-readiness', '资金占用', '机会列表', '六态状态计数'];
      for (const marker of ['[aria-label="资金占用"]', '.pm-opportunity', '.pm-relation-drawer']) {
        await expect(page.locator(marker).first()).toBeVisible();
      }
      const positions = await root.evaluate((element, labels) => {
        const html = element.innerHTML;
        return labels.map((label) => html.indexOf(label)).filter((index) => index >= 0);
      }, markers);
      expect(positions.length).toBe(markers.length);
      expect(positions.every((index, i) => i === 0 || positions[i - 1] < index)).toBe(true);
      await expect(page.locator('.pm-mode-button').first()).toHaveText('MANUAL');
      await expect(page.locator('.pm-mode-button').nth(1)).toHaveText('AUTO');
      await expect(page.locator('.pm-venue-card')).toHaveCount(2);
      await expect(page.locator('[aria-label="资金占用"]')).toContainText('max_total_unsettled_capital');
      await expect(page.locator('.pm-opportunity')).toHaveCount(1);
      await expect(page.locator('.pm-opportunity').getByRole('button', { name: '人工确认下单' })).toBeVisible();
      await expect(page.locator('.pm-relation-drawer')).toBeVisible();
      expect(await page.locator('body').evaluate((body) => body.scrollWidth <= window.innerWidth)).toBe(true);
    }
  });

  test('filters the unified opportunity list without strategy tabs', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1100 });
    await openPrediction(page);
    await expect(page.locator('.pm-strategy-tabs')).toContainText('全部');
    await expect(page.locator('body')).not.toContainText('YES/NO套利');
    await expect(page.locator('body')).not.toContainText('LLM对冲套利');
    const list = page.locator('[aria-label="机会列表"]');
    await list.getByRole('button', { name: 'LLM', exact: true }).click();
    await expect(page.locator('.pm-opportunity')).toHaveCount(0);
    await expect(page.locator('[aria-label="机会列表"] .pm-empty')).toContainText('当前无更多合格机会');
    await list.getByRole('button', { name: '全部', exact: true }).click();
    await expect(page.locator('.pm-opportunity')).toHaveCount(1);
  });

  test('MANUAL confirm opens the existing cross-venue confirmation modal', async ({ page }) => {
    const previewRequests: string[] = [];
    const confirmRequests: string[] = [];
    page.on('request', (request) => {
      if (request.url().includes('/prediction-arbitrage/preview')) previewRequests.push(request.method());
      if (request.url().includes('/prediction-arbitrage/executions')) confirmRequests.push(request.method());
    });
    await page.setViewportSize({ width: 1440, height: 1100 });
    await openPrediction(page);
    await page.getByRole('button', { name: '人工确认下单' }).click();
    await expect(page.locator('.pm-modal')).toBeVisible();
    for (const text of ['Predict.fun · BUY YES', 'Polymarket · BUY NO', '确认下单', '不是原子交易']) {
      await expect(page.locator('.pm-modal')).toContainText(text);
    }
    await page.getByRole('button', { name: /确认下单 · 最多/ }).dblclick();
    await expect.poll(() => confirmRequests.length).toBe(1);
    expect(previewRequests.length).toBeGreaterThanOrEqual(1);
  });

  test('relation review overview opens the six-state drawer', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1100 });
    await openPrediction(page);
    const overview = page.locator('[aria-label="关系审核概览"]');
    await expect(overview).toBeVisible();
    for (const label of ['待批准', '已批准 · 模型不完整', '编译补全待激活', '激活阻断', '已激活', '来源变化需重批']) {
      await expect(overview.locator('.pm-relation-chip', { hasText: label })).toBeVisible();
    }

    await page.locator('[data-open-relation-view="activation_blocked"]').click();
    const drawer = page.locator('.pm-relation-drawer[role="dialog"]');
    await expect(drawer).toBeVisible();
    for (const label of ['待批准', '模型不完整', '编译补全待激活', '激活阻断', '已激活', '需重批']) {
      await expect(drawer.locator('.pm-relation-tabs')).toContainText(label);
    }
    await expect(drawer.locator('.pm-relation-pager')).toContainText('显示 1–1 / 1');

    await page.locator('[data-action="close-relation-review"]').click();
    await expect(drawer).toHaveCount(0);
    await page.locator('[data-action="open-relation-review"]').first().click();
    await expect(page.locator('.pm-relation-drawer[role="dialog"]')).toBeVisible();
    await expect(page.locator('.pm-relation-drawer[role="dialog"] .pm-relation-list')).toContainText('必须为 YES');
  });

  test('captures the unified page screenshot for mock parity', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1100 });
    await openPrediction(page);
    await expect(page.locator('.pm-page-head')).toContainText('contract generation 1');
    await expect(page.locator('.pm-opportunity')).toContainText('+$8.40');
    await expect(page.locator('.pm-opportunity')).toContainText('24.5%');
    await expect(page.locator('.pm-opportunity')).toContainText('28.4%');
    await expect(page.locator('.pm-opportunity')).toContainText('12 天');
    await expect(page.locator('.pm-opportunity')).toContainText('-$0.80');
    await page.screenshot({ path: 'test-results/prediction-unified.png', fullPage: true });
  });
});
