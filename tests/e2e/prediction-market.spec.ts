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

  test('renders the read-only observation coverage and ROI projection', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1100 });
    await openPrediction(page);
    const coverage = page.locator('[aria-label="观测覆盖"]');
    await expect(coverage).toBeVisible();
    await expect(coverage).toContainText('持续观测池');
    await expect(coverage).toContainText(/池内\s*2\s*\/\s*10/);
    await expect(coverage).toContainText(/最新\s*3/);
    await expect(coverage).toContainText(/新鲜\s*2/);
    await expect(coverage).toContainText(/正收益\s*1/);
    await expect(coverage).toContainText(/非正收益\s*1/);
    await expect(coverage).toContainText('净 ROI');
    await expect(coverage.locator('button')).toHaveCount(0);
  });

  test('observation coverage shows five cached states and read-only details', async ({ page }) => {
    const states = [
      ['ready', '已就绪', '2 / 10', '3'],
      ['observation-empty', '明确为空', '0 / 10', '0'],
      ['observation-stale', '数据过期', '2 / 10', '3'],
      ['observation-error', '读取失败', '2 / 10', '3'],
      ['observation-unknown', '未知', '—', '—'],
    ] as const;
    const methods: string[] = [];
    page.on('request', (request) => {
      if (request.url().includes('/api/prediction-arbitrage/')) methods.push(request.method());
    });
    for (const viewport of [{ width: 1440, height: 1100 }, { width: 375, height: 812 }]) {
      await page.setViewportSize(viewport);
      for (const [scenario, status, capacity, latest] of states) {
        await openPrediction(page, scenario);
        const coverage = page.locator('[aria-label="观测覆盖"]');
        await expect(coverage).toBeVisible();
        await expect(coverage.locator('[data-observation-status]')).toHaveText(status);
        await expect(coverage).toContainText(`池内${capacity.replace(/\s/g, '')}`);
        await expect(coverage).toContainText(`最新${latest}`);
        await expect(coverage.locator('button')).toHaveCount(0);
      }
    }
    await openPrediction(page, 'ready');
    const coverage = page.locator('[aria-label="观测覆盖"]');
    for (const label of ['来源范围', '待准备', '排除', '原生', '三腿', '已订阅 token', '全部腿已订阅组', '最旧盘口', '最近尝试', '最近成功', '数量', '含费成本', '最低赔付', '净额', '阻断原因']) {
      await expect(coverage).toContainText(label);
    }
    await expect(coverage.locator('table[aria-label="观测结果"] thead th')).toHaveCount(16);
    await expect(coverage.locator('details[data-observation-details]').first()).toBeVisible();
    await coverage.locator('[data-observation-filter]').selectOption('blocked');
    await expect(coverage.locator('table[aria-label="观测结果"] tbody tr')).toHaveCount(0);
    expect(methods.every((method) => method === 'GET')).toBe(true);
  });

  test('observation coverage retains rows after a failed state fetch as stale and not current', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1100 });
    await openPrediction(page, 'observation-fetch-error');
    const coverage = page.locator('[aria-label="观测覆盖"]');
    await expect(coverage.locator('[data-observation-status]')).toHaveText('已就绪');
    await expect(coverage).toContainText(/新鲜\s*2/);
    await expect(coverage).toContainText('同条件 YES/NO 观察市场');

    await page.evaluate(async () => {
      await fetchPredictionState();
    });

    await expect(coverage.locator('[data-observation-status]')).toHaveText('读取失败');
    await expect(coverage).toContainText(/新鲜\s*0/);
    await expect(coverage).toContainText(/正收益\s*0/);
    await expect(coverage).toContainText(/非正收益\s*0/);
    await expect(coverage).toContainText('同条件 YES/NO 观察市场');
    await expect(coverage).toContainText('资本释放');
    await expect(coverage).toContainText('已超过预计结束时间');
    await expect(coverage).toContainText('未知');
    await expect(coverage.locator('button')).toHaveCount(0);
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
    await expect(page.locator('.pm-opportunity')).not.toContainText('极端风险');
    await page.screenshot({ path: 'test-results/prediction-unified.png', fullPage: true });
  });
});
