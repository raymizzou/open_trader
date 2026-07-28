import { expect, test, type Page } from '@playwright/test';

// Goldens are intentionally tied to the user-approved prototype commit.
const APPROVED_PROTOTYPE_SHA = 'e0d5083';
const PROTOTYPE_BASE_URL = process.env.PREDICTION_UI_BASE_URL ?? 'http://127.0.0.1:8772/prediction-market-execution-prototype.html';
const GOLDEN_SOURCE = `${PROTOTYPE_BASE_URL} @ ${APPROVED_PROTOTYPE_SHA}; deterministic six-event fixture is kept in serve_dashboard_fixture.py`;
const CAPTURE_PROTOTYPE = process.env.PREDICTION_UI_CAPTURE === '1';
const states = [
  'loading', 'ready', 'quiet', 'executing', 'success', 'incident',
  'degraded', 'confirmation', 'reset', 'history-signals',
  'history-executions', 'history-incidents',
] as const;
const viewports = [
  { name: 'desktop', width: 1440, height: 1100 },
  { name: 'mobile', width: 375, height: 812 },
] as const;

async function openPrediction(page: Page, state: string) {
  if (CAPTURE_PROTOTYPE) {
    const { state: prototypeState, history } = prototypeStateFor(state);
    await page.goto(`${PROTOTYPE_BASE_URL}?state=${prototypeState}&history=${history}`, { waitUntil: 'networkidle' });
    await page.addStyleTag({ content: '.pm-controller { display: none !important; } body { padding-bottom: 0 !important; }' });
    await expect(page.getByRole('heading', { name: '预测市场套利' })).toBeVisible();
    return;
  }
  await page.goto(`/?prediction_state=${state}`, { waitUntil: 'networkidle' });
  await page.getByRole('button', { name: '预测市场', exact: true }).click();
  await expect(page.locator('#prediction-market-workspace')).toBeVisible();
  await expect(page.getByRole('heading', { name: '预测市场套利' })).toBeVisible();
}

function prototypeStateFor(state: string) {
  if (state === 'confirmation') return { state: 'ready', history: 'signals' };
  if (state === 'reset') return { state: 'incident', history: 'incidents' };
  if (state === 'history-signals') return { state: 'ready', history: 'signals' };
  if (state === 'history-executions') return { state: 'success', history: 'trades' };
  if (state === 'history-incidents') return { state: 'incident', history: 'incidents' };
  if (state === 'success') return { state: 'success', history: 'trades' };
  if (state === 'incident') return { state: 'incident', history: 'incidents' };
  return { state, history: 'signals' };
}

test.describe('approved prediction execution workspace', () => {
  test('keeps the top navigation and workspace order exact', async ({ page }) => {
    await openPrediction(page, 'ready');
    await expect(page.locator('.dashboard-header')).toBeHidden();
    await expect(page.locator('.pm-nav button')).toHaveText(['持仓', '预测市场', '策略回测', '凯利实验室']);
    for (const label of ['交易钱包', '可用余额', '地区与连接', '实盘状态', '首单验证']) {
      await expect(page.locator('.pm-readiness')).toContainText(label);
    }
    await expect(page.locator('.pm-panel').nth(0)).toContainText('当前监控范围');
    await expect(page.locator('.pm-panel').nth(1)).toContainText('当前机会');
    await expect(page.locator('.pm-panel').nth(2)).toContainText('历史记录');
    await expect(page.locator('body')).toContainText('24h 成交量');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    expect(APPROVED_PROTOTYPE_SHA).toBe('e0d5083');
    expect(PROTOTYPE_BASE_URL).toContain('prediction-market-execution-prototype.html');
  });

  test('participation preview and confirmation submit exactly once', async ({ page }) => {
    await openPrediction(page, 'confirmation');
    const previewRequests: string[] = [];
    const confirmRequests: string[] = [];
    page.on('request', (request) => {
      if (request.url().includes('/prediction-arbitrage/preview')) previewRequests.push(request.method());
      if (request.url().includes('/prediction-arbitrage/executions')) confirmRequests.push(request.method());
    });
    const trigger = page.locator('[data-action="participate"]');
    await trigger.click();
    await expect(page.locator('.pm-modal')).toBeVisible();
    for (const copy of ['确认真实下单', '$20', '$2', '免手续费']) {
      await expect(page.locator('.pm-modal')).toContainText(copy);
    }
    await page.keyboard.press('Escape');
    await expect(page.locator('.pm-modal')).toHaveCount(0);
    await expect(trigger).toBeFocused();
    await trigger.click();
    await page.route('**/api/prediction-arbitrage/executions**', async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 350));
      await route.continue();
    });
    await page.getByRole('button', { name: /确认下单/ }).click();
    await expect(page.locator('.pm-modal button:disabled')).toHaveCount(2);
    await expect(trigger).toBeDisabled();
    await expect.poll(() => confirmRequests.length).toBe(1);
    expect(previewRequests).toEqual(['POST', 'POST']);
    await expect(page.locator('.pm-alert')).toBeVisible();
  });

  test('orders events by actionability and exposes the volume used for ranking', async ({ page }) => {
    await openPrediction(page, 'ready');
    await expect(page.locator('.pm-event-title')).toHaveText([
      '以色列与伊朗停火是否持续至 8 月 31 日？',
      '比特币会在 8 月突破 $150,000？',
      '2026 年 9 月美联储是否降息？',
      '以太坊会在 9 月前突破 $6,000？',
      '2026 年美国参议院控制权',
      '下一任美联储主席人选',
    ]);
    const expectedVolumes = ['$9.7M', '$12.8M', '$15.4M', '$7.1M', '$6.8M', '$5.9M'];
    for (const [index, volume] of expectedVolumes.entries()) {
      await expect(page.locator('.pm-volume').nth(index)).toContainText('24h 成交量');
      await expect(page.locator('.pm-volume').nth(index)).toContainText(volume);
    }
  });

  test('does not open an order modal when the preview is rejected', async ({ page }) => {
    await openPrediction(page, 'preview-rejected');
    const executionRequests: string[] = [];
    page.on('request', (request) => {
      if (request.url().includes('/prediction-arbitrage/executions')) executionRequests.push(request.method());
    });
    await page.locator('[data-action="participate"]').click();
    await expect(page.locator('.pm-modal')).toHaveCount(0);
    await expect(page.getByRole('alert')).toContainText('机会已变化或已失效');
    expect(executionRequests).toEqual([]);
  });

  test('keeps the reset modal open when the backend denies reset', async ({ page }) => {
    await openPrediction(page, 'reset-denied');
    await page.locator('[data-action="open-reset"]').click();
    await page.getByRole('button', { name: '我已处理，恢复交易' }).click();
    await expect(page.locator('.pm-modal')).toBeVisible();
    await expect(page.getByRole('alert').filter({ hasText: '本次操作未提交' })).toBeVisible();
    await expect(page.locator('[data-action="open-reset"]')).toBeVisible();
  });

  test('maps the live opportunity and event field names into the approved UI', async ({ page }) => {
    await page.route('**/api/prediction-arbitrage/state*', async (route) => {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          status: 'healthy',
          readiness: { status: 'ready', balance: '50.00', geoblock: '允许交易', first_live_order: '待首单' },
          event_count: 1,
          market_count: 1,
          token_count: 2,
          signals_24h: 1,
          events: [{
            event_id: 'live-event', question: '真实问题字段', volume_24h: 1234567,
            markets: [{ question: '真实市场字段', actionable: true, yes_max_price: '0.401', no_max_price: '0.499' }],
          }],
          opportunities: [{
            opportunity_id: 'live-opp', question: '真实问题字段', actionable: true,
            yes_max_price: '0.401', no_max_price: '0.499', yes_max_cost: '8.02',
            no_max_cost: '9.98', total_max_cost: '18.00', minimum_profit: '2.00', quantity: '20',
          }],
          current_execution: { state: 'submitting', question: '真实问题字段', execution_id: 'live-exec' },
          histories: { signals: [] },
          breaker: { open: false },
          csrf_token: 'fixture-csrf',
        }),
      });
    });
    await page.goto('/?prediction_state=ready', { waitUntil: 'networkidle' });
    await page.getByRole('button', { name: '预测市场', exact: true }).click();
    await expect(page.locator('.pm-event-title')).toHaveText(['真实问题字段']);
    await expect(page.locator('.pm-opportunity h3')).toHaveText('真实问题字段');
    await expect(page.locator('.pm-opportunity')).toContainText('$0.401');
    await expect(page.locator('.pm-opportunity')).toContainText('$18.00');
    await expect(page.locator('.pm-opportunity')).toContainText('+$2.00');
    await expect(page.locator('.pm-progress')).toBeVisible();
    await expect(page.locator('.pm-readiness')).toContainText('执行中');
  });

  test('incident reset and history tabs remain visible', async ({ page }) => {
    await openPrediction(page, 'reset');
    await expect(page.locator('[data-action="open-reset"]')).toBeVisible();
    await page.locator('[data-action="open-reset"]').click();
    await expect(page.locator('.pm-modal')).toContainText('确认解除交易熔断');
    await page.getByRole('button', { name: '我已处理，恢复交易' }).click();
    await expect(page.locator('[data-action="open-reset"]')).toHaveCount(0);
    for (const [kind, label] of [['signals', '信号历史'], ['executions', '交易与合并'], ['incidents', '事故']] as const) {
      await page.getByRole('button', { name: label, exact: true }).click();
      await expect(page.locator(`[data-history="${kind}"]`)).toHaveAttribute('aria-pressed', 'true');
    }
  });

  test('keeps the approved state-specific copy and six-column histories', async ({ page }) => {
    await openPrediction(page, 'incident');
    await expect(page.locator('.pm-alert.danger')).toContainText('交易已熔断：发生单腿成交');
    for (const copy of ['YES 成交、NO 被拒', '$0.60', 'macOS 与飞书已通知']) {
      await expect(page.locator('.pm-alert.danger')).toContainText(copy);
    }
    await openPrediction(page, 'executing');
    for (const copy of ['双腿提交', '2 笔 FOK 已签名', '成交核对']) {
      await expect(page.locator('.pm-progress')).toContainText(copy);
    }
    await openPrediction(page, 'success');
    for (const copy of ['实际成本 $18.80', '合并收回 $20.00', '+$1.20', '14:36:12']) {
      await expect(page.locator('.pm-alert.success')).toContainText(copy);
    }
    await openPrediction(page, 'quiet');
    await expect(page.locator('.pm-stack > .pm-panel').first().locator('.pm-empty')).toContainText('当前没有可参与机会');
    await expect(page.locator('.pm-opportunity')).toHaveCount(0);
    await openPrediction(page, 'degraded');
    await expect(page.locator('.pm-opportunity.disabled')).toBeVisible();
    await openPrediction(page, 'loading');
    await expect(page.locator('.pm-event')).toHaveCount(6);
    await expect(page.locator('.pm-stack > .pm-panel').first().locator('.pm-empty')).toContainText('正在读取可参与机会');
    await openPrediction(page, 'history-signals');
    await page.locator('[data-history="signals"]').click();
    await expect(page.locator('.pm-table th')).toHaveCount(6);
  });

  test('[UI-01] desktop prototype parity', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1100 });
    await openPrediction(page, 'ready');
    await expect(page.locator('.pm-page-head h1')).toHaveText('预测市场套利');
    await expect(page.locator('.pm-event')).toHaveCount(6);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });

  test('[UI-02] mobile prototype parity', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 });
    await openPrediction(page, 'ready');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    for (const height of await page.locator('#prediction-market-workspace button:visible').evaluateAll((elements) => elements.map((element) => element.getBoundingClientRect().height))) {
      expect(height).toBeGreaterThanOrEqual(44);
    }
  });

  test('[UI-03] keyboard modal behavior', async ({ page }) => {
    await openPrediction(page, 'confirmation');
    const trigger = page.locator('[data-action="participate"]');
    await trigger.click();
    await expect(page.locator('.pm-modal')).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(page.locator('.pm-modal')).toHaveCount(0);
    await expect(trigger).toBeFocused();
  });

  test('[UI-04] status semantics', async ({ page }) => {
    await openPrediction(page, 'incident');
    await expect(page.locator('.pm-alert.danger')).toContainText('交易已熔断');
    await openPrediction(page, 'degraded');
    await expect(page.locator('.pm-alert.danger')).toContainText('数据连接异常');
    await expect(page.locator('.pm-opportunity button')).toHaveText('数据异常');
  });

  test('[UI-05] cost disclosure', async ({ page }) => {
    await openPrediction(page, 'confirmation');
    await page.locator('[data-action="participate"]').click();
    await expect(page.locator('.pm-modal')).toContainText('$65');
    await expect(page.locator('.pm-modal')).toContainText('$20');
    await expect(page.locator('.pm-modal')).toContainText('$2');
  });

  for (const viewport of viewports) {
    for (const state of states) {
      test(`${state} golden · ${viewport.name}`, async ({ page }) => {
        test.info().annotations.push({ type: 'golden-source', description: GOLDEN_SOURCE });
        await page.setViewportSize({ width: viewport.width, height: viewport.height });
        const consoleErrors: string[] = [];
        const httpErrors: string[] = [];
        page.on('console', (message) => { if (message.type() === 'error') consoleErrors.push(message.text()); });
        page.on('response', (response) => { if (response.status() >= 500) httpErrors.push(`${response.status()} ${response.url()}`); });
        await openPrediction(page, state);
        if (state === 'confirmation') await page.locator('[data-action="participate"]').click();
        if (state === 'reset') await page.locator('[data-action="open-reset"]').click();
        if (!CAPTURE_PROTOTYPE && state.startsWith('history-')) {
          const kind = state.replace('history-', '');
          await Promise.all([
            page.waitForResponse((response) => response.url().includes(`/api/prediction-arbitrage/history?kind=${kind}`)),
            page.locator(`[data-history="${kind}"]`).click(),
          ]);
          await expect(page.locator('.pm-panel').last()).toContainText('今天');
        }
        await expect(page).toHaveScreenshot(`prediction-market-${state}-${viewport.name}.png`, {
          animations: 'disabled',
          fullPage: true,
          maxDiffPixelRatio: 0.001,
        });
        expect(consoleErrors, `console errors in ${state}`).toEqual([]);
        expect(httpErrors, `HTTP errors in ${state}`).toEqual([]);
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
        if (viewport.name === 'mobile') {
          for (const height of await page.locator('#prediction-market-workspace button:visible').evaluateAll((elements) => elements.map((element) => element.getBoundingClientRect().height))) {
            expect(height).toBeGreaterThanOrEqual(44);
          }
        }
      });
    }
  }

});
