import { expect, test, type Page } from '@playwright/test';

async function openPrediction(page: Page, state = 'ready') {
  await page.goto(`/?prediction_state=${state}`, { waitUntil: 'networkidle' });
  await page.getByRole('button', { name: '预测市场', exact: true }).click();
  await expect(page.locator('#prediction-market-workspace')).toBeVisible();
  await expect(page.getByRole('heading', { name: '预测市场' })).toBeVisible();
  await page.getByRole('tab', { name: '多腿套利', exact: true }).click();
  await expect(page.getByRole('heading', { name: '多腿套利' })).toBeVisible();
}

test.describe('unified N_LEG opportunity page', () => {
  test('renders the approved mock blocks in order on desktop and mobile', async ({ page }) => {
    for (const viewport of [{ width: 1440, height: 1100 }, { width: 375, height: 812 }]) {
      await page.setViewportSize(viewport);
      await openPrediction(page);
      const root = page.locator('#prediction-market-workspace');
      const markers = ['pm-page-head', 'pm-venue-readiness', 'pm-strategy-tabs', 'pm-mode-bar', '资金占用', '机会列表', '六态状态计数'];
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

  test('filters the multi-leg opportunity list within its tab', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1100 });
    await openPrediction(page);
    const filters = page.locator('[aria-label="机会列表"] .pm-strategy-tabs');
    await expect(filters).toContainText('全部');
    await expect(page.locator('body')).not.toContainText('YES/NO套利');
    await expect(page.locator('body')).not.toContainText('LLM对冲套利');
    const list = page.locator('[aria-label="机会列表"]');
    await filters.getByRole('button', { name: 'LLM', exact: true }).click();
    await expect(page.locator('.pm-opportunity')).toHaveCount(0);
    await expect(page.locator('[aria-label="机会列表"] .pm-empty')).toContainText('当前无更多合格机会');
    await filters.getByRole('button', { name: '全部', exact: true }).click();
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
    await expect(coverage).toContainText('净收益率');
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
    for (const label of ['来源范围', '待准备', '排除', '原生', '三腿', '已订阅 token', '全部腿已订阅组', '净收益率', '净收益', '当前状态', '含费成本', '最低赔付']) {
      await expect(coverage).toContainText(label);
    }
    await expect(coverage.locator('table[aria-label="观测结果"] thead th')).toHaveText([
      '标的名称', '净收益率', '净收益', '当前状态', '预计结束时间', '含费成本', '最低赔付',
    ]);
    const details = coverage.locator('details[data-observation-details]').first();
    await expect(details).toBeVisible();
    await details.locator('summary').click();
    for (const label of ['类型', '版本', '审批', '数量', '最近尝试', '最近成功', '资本释放', '释放状态', '来源范围']) {
      await expect(details).toContainText(label);
    }
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
    await expect(coverage).toContainText('状态读取失败');
    await expect(coverage.locator('button')).toHaveCount(0);
  });

  test('observation table prioritizes market titles and economics without overflow', async ({ page }) => {
    for (const viewport of [{ width: 1440, height: 1100 }, { width: 375, height: 812 }]) {
      await page.setViewportSize(viewport);
      await openPrediction(page, 'observation-table-titles');
      const coverage = page.locator('[aria-label="观测覆盖"]');
      const table = coverage.locator('table[aria-label="观测结果"]');
      await expect(table.locator('thead th')).toHaveText([
        '标的名称', '净收益率', '净收益', '当前状态', '预计结束时间', '含费成本', '最低赔付',
      ]);
      await expect(table.locator('tbody tr')).toHaveCount(2);
      await expect(table).toContainText('Will Aurora win the full game?');
      await expect(table).toContainText('Will Aurora win the first half?');
      await expect(table).toContainText('+2.04%');
      await expect(table).toContainText('+$0.10');
      await expect(table).toContainText('$4.90');
      await expect(table).toContainText('$5.00');
      const visibleTableText = await table.evaluate((element) => (element as HTMLElement).innerText);
      expect(visibleTableText).not.toContain('version-' + 'x'.repeat(180));
      expect(visibleTableText).not.toContain('APPROVED');
      const details = table.locator('details[data-observation-details]').first();
      await expect(details.locator('dl')).toBeHidden();
      const closedGeometry = await page.evaluate(() => {
        const row = document.querySelector('[aria-label="观测结果"] tbody tr');
        const name = row?.querySelector('td[data-label="标的名称"] strong') as HTMLElement | null;
        const roi = row?.querySelector('td[data-label="净收益率"] strong') as HTMLElement | null;
        return {
          viewport: window.innerWidth,
          rowHeight: row?.getBoundingClientRect().height ?? 0,
          nameWidth: name?.getBoundingClientRect().width ?? 0,
          roiWidth: roi?.getBoundingClientRect().width ?? 0,
          captionWidth: (document.querySelector('[aria-label="观测结果"] caption') as HTMLElement | null)?.getBoundingClientRect().width ?? 0,
          captionHeight: (document.querySelector('[aria-label="观测结果"] caption') as HTMLElement | null)?.getBoundingClientRect().height ?? 0,
        };
      });
      expect(closedGeometry.nameWidth).toBeGreaterThan(closedGeometry.viewport === 375 ? 180 : 250);
      expect(closedGeometry.roiWidth).toBeGreaterThan(40);
      expect(closedGeometry.captionWidth).toBeGreaterThan(closedGeometry.viewport === 375 ? 180 : 250);
      expect(closedGeometry.captionHeight).toBeLessThan(60);
      expect(closedGeometry.rowHeight).toBeLessThan(closedGeometry.viewport === 375 ? 700 : 500);
      const closedOverflow = await page.evaluate(() => {
        const observation = document.querySelector('[aria-label="观测覆盖"]') as HTMLElement | null;
        return {
          page: document.documentElement.scrollWidth <= window.innerWidth && document.body.scrollWidth <= window.innerWidth,
          observation: Boolean(observation) && observation.scrollWidth <= observation.clientWidth,
        };
      });
      expect(closedOverflow).toEqual({ page: true, observation: true });
      await details.getByText('详情').click();
      await expect(details.locator('dl')).toBeVisible();
      await expect(details).toContainText('version-' + 'x'.repeat(180));
      await expect(details).toContainText('APPROVED');
      const geometry = await page.evaluate(() => {
        const row = document.querySelector('[aria-label="观测结果"] tbody tr');
        const name = row?.querySelector('td[data-label="标的名称"] strong') as HTMLElement | null;
        const roi = row?.querySelector('td[data-label="净收益率"] strong') as HTMLElement | null;
        return {
          viewport: window.innerWidth,
          rowHeight: row?.getBoundingClientRect().height ?? 0,
          nameWidth: name?.getBoundingClientRect().width ?? 0,
          nameHeight: name?.getBoundingClientRect().height ?? 0,
          roiWidth: roi?.getBoundingClientRect().width ?? 0,
          roiHeight: roi?.getBoundingClientRect().height ?? 0,
          captionWidth: (document.querySelector('[aria-label="观测结果"] caption') as HTMLElement | null)?.getBoundingClientRect().width ?? 0,
          captionHeight: (document.querySelector('[aria-label="观测结果"] caption') as HTMLElement | null)?.getBoundingClientRect().height ?? 0,
        };
      });
      expect(geometry.nameWidth).toBeGreaterThan(closedGeometry.viewport === 375 ? 180 : 250);
      expect(geometry.roiWidth).toBeGreaterThan(40);
      expect(geometry.captionWidth).toBeGreaterThan(closedGeometry.viewport === 375 ? 180 : 250);
      expect(geometry.captionHeight).toBeLessThan(60);
      expect(geometry.rowHeight).toBeLessThan(closedGeometry.viewport === 375 ? 1800 : 800);
      const overflow = await page.evaluate(() => {
        const observation = document.querySelector('[aria-label="观测覆盖"]') as HTMLElement | null;
        return {
          page: document.documentElement.scrollWidth <= window.innerWidth && document.body.scrollWidth <= window.innerWidth,
          observation: Boolean(observation) && observation.scrollWidth <= observation.clientWidth,
        };
      });
      expect(overflow).toEqual({ page: true, observation: true });
      await page.screenshot({
        path: `test-results/observation-table-${geometry.viewport === 375 ? 'mobile' : 'desktop'}-expanded.png`,
        fullPage: true,
      });
    }
  });

  test('observation table shows pool members and separates waiting without excluded rows', async ({ page }) => {
    const methods: string[] = [];
    page.on('request', (request) => {
      if (request.url().includes('/api/prediction-arbitrage/')) methods.push(request.method());
    });
    await openPrediction(page, 'observation-table-membership');
    const coverage = page.locator('[aria-label="观测覆盖"]');
    const table = coverage.locator('table[aria-label="观测结果"]');
    const filter = coverage.locator('[data-observation-filter]');
    await expect(filter.locator('option')).toHaveText(['全部（池内）', '当前有效', '阻断 / 陈旧', '等待入池']);
    await expect(filter).toHaveValue('all');
    await expect(coverage).toContainText(/排除\s*2/);
    await expect(table.locator('tbody tr')).toHaveCount(3);
    for (const title of ['Pool positive market', 'Pool negative market', 'Pool blocked market']) {
      await expect(table).toContainText(title);
    }
    for (const title of ['Waiting market', 'Excluded current must stay hidden', 'Rejected stale must stay hidden']) {
      await expect(table).not.toContainText(title);
    }
    const positiveDetails = table.locator('tbody tr', { hasText: 'Pool positive market' }).locator('details[data-observation-details]');
    await positiveDetails.locator('summary').click();
    await expect(positiveDetails.locator('dl')).toContainText('订阅 token');
    await expect(positiveDetails.locator('dl')).toContainText('2');

    await filter.selectOption('waiting');
    await expect(table.locator('tbody tr')).toHaveCount(1);
    await expect(table).toContainText('Waiting market');
    await expect(table).not.toContainText('Pool positive market');

    await filter.selectOption('current');
    await expect(table.locator('tbody tr')).toHaveCount(2);
    await expect(table).toContainText('Pool positive market');
    await expect(table).toContainText('Pool negative market');
    await expect(table).not.toContainText('Waiting market');
    await expect(table).not.toContainText('Excluded current must stay hidden');

    await filter.selectOption('blocked');
    await expect(table.locator('tbody tr')).toHaveCount(1);
    await expect(table).toContainText('Pool blocked market');
    await expect(table).not.toContainText('Rejected stale must stay hidden');

    await openPrediction(page, 'observation-table-pending-quote');
    const pendingTable = page.locator('[aria-label="观测覆盖"] table[aria-label="观测结果"]');
    const pendingFilter = page.locator('[aria-label="观测覆盖"] [data-observation-filter]');
    await expect(pendingTable.locator('tbody tr')).toHaveCount(1);
    await expect(pendingTable).toContainText('Pending quote pool member');
    await pendingFilter.selectOption('current');
    await expect(pendingTable.locator('tbody tr')).toHaveCount(0);
    await pendingFilter.selectOption('waiting');
    await expect(pendingTable.locator('tbody tr')).toHaveCount(0);
    await pendingFilter.selectOption('blocked');
    await expect(pendingTable.locator('tbody tr')).toHaveCount(0);

    await openPrediction(page, 'observation-table-identity-fallback');
    const identityTable = page.locator('[aria-label="观测覆盖"] table[aria-label="观测结果"]');
    await expect(identityTable.locator('tbody tr')).toHaveCount(1);
    await expect(identityTable).toContainText('关系 ID：relation-only-fallback');
    await expect(identityTable).not.toContainText('事件 ID：relation-only-fallback');
    expect(methods.every((method) => method === 'GET')).toBe(true);
  });

  test('observation table distinguishes historical and unavailable economics', async ({ page }) => {
    await openPrediction(page, 'observation-table-history');
    const table = page.locator('[aria-label="观测覆盖"] table[aria-label="观测结果"]');
    const current = table.locator('tbody tr', { hasText: 'Current positive market' });
    const stale = table.locator('tbody tr', { hasText: 'Stale previous positive market' });
    const unavailable = table.locator('tbody tr', { hasText: 'Blocked never computed market' });
    await expect(current).toHaveCount(1);
    await expect(current).not.toContainText('上次结果');
    await expect(current.locator('[data-label="净收益率"]')).toContainText('+2.04%');
    await expect(current.locator('[data-label="净收益"]')).toContainText('+$0.10');
    await expect(current.locator('[data-label="当前状态"]')).toContainText('当前有效');
    await expect(stale).toContainText('上次结果');
    await expect(stale).toContainText('+2.04%');
    await expect(stale.locator('[data-label="当前状态"]')).toContainText('已阻断');
    await expect(unavailable.locator('[data-label="净收益率"]')).toHaveText('—');
    await expect(unavailable.locator('[data-label="净收益"]')).toHaveText('—');
    await expect(unavailable.locator('[data-label="含费成本"]')).toHaveText('—');
    await expect(unavailable.locator('[data-label="最低赔付"]')).toHaveText('—');
    await expect(unavailable).not.toContainText('上次结果');
    await expect(unavailable.locator('[data-label="当前状态"]')).toContainText('已阻断');
    await unavailable.locator('summary').click();
    await expect(unavailable.locator('dl')).toContainText('UNKNOWN_MODEL_FACTS');

    await openPrediction(page, 'observation-error');
    const errorTable = page.locator('[aria-label="观测覆盖"] table[aria-label="观测结果"]');
    await expect(errorTable.locator('tbody tr')).toHaveCount(2);
    await expect(errorTable.locator('tbody tr').first()).toContainText('上次结果');
    await expect(errorTable.locator('tbody tr').first().locator('[data-label="当前状态"]')).toContainText('读取失败');

    await openPrediction(page, 'observation-fetch-error');
    const fetchErrorCoverage = page.locator('[aria-label="观测覆盖"]');
    const fetchErrorTable = fetchErrorCoverage.locator('table[aria-label="观测结果"]');
    await expect(fetchErrorTable.locator('tbody tr')).toHaveCount(2);
    await page.evaluate(async () => {
      await fetchPredictionState();
    });
    await expect(fetchErrorCoverage.locator('[data-observation-status]')).toHaveText('读取失败');
    await expect(fetchErrorTable.locator('tbody tr')).toHaveCount(2);
    await expect(fetchErrorTable.locator('tbody tr').first()).toContainText('上次结果');
    await expect(fetchErrorTable.locator('tbody tr').first().locator('[data-label="当前状态"]')).toContainText('读取失败');
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
    await expect(page.locator('#prediction-market-panel-multi-leg .pm-page-head')).toContainText('contract generation 1');
    await expect(page.locator('.pm-opportunity')).toContainText('+$8.40');
    await expect(page.locator('.pm-opportunity')).toContainText('24.5%');
    await expect(page.locator('.pm-opportunity')).toContainText('28.4%');
    await expect(page.locator('.pm-opportunity')).toContainText('12 天');
    await expect(page.locator('.pm-opportunity')).not.toContainText('极端风险');
    await page.screenshot({ path: 'test-results/prediction-unified.png', fullPage: true });
  });
});
