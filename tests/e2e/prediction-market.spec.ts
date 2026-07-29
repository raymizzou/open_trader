import { expect, test, type Page } from '@playwright/test';

// Goldens are intentionally tied to the user-approved prototype commit.
const APPROVED_PROTOTYPE_SHA = '12d3391';
const PROTOTYPE_BASE_URL = process.env.PREDICTION_UI_BASE_URL ?? 'http://127.0.0.1:8773/prediction-market-truthful-ui-prototype.html';
const GOLDEN_SOURCE = `${PROTOTYPE_BASE_URL} @ ${APPROVED_PROTOTYPE_SHA}; deterministic six-event fixture is kept in serve_dashboard_fixture.py`;
const CAPTURE_PROTOTYPE = process.env.PREDICTION_UI_CAPTURE === '1';
const states = [
  'loading', 'ready', 'quiet', 'incomplete', 'executing', 'success',
  'success-incomplete', 'incident', 'incident-incomplete', 'degraded',
  'unavailable', 'unknown', 'confirmation', 'reset', 'history-signals',
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
  if (state === 'incomplete' || state === 'unavailable' || state === 'unknown') return { state: 'unavailable', history: 'signals' };
  if (state === 'success-incomplete') return { state: 'success', history: 'trades' };
  if (state === 'incident-incomplete') return { state: 'incident', history: 'incidents' };
  if (state === 'history-signals') return { state: 'ready', history: 'signals' };
  if (state === 'history-executions') return { state: 'success', history: 'trades' };
  if (state === 'history-incidents') return { state: 'incident', history: 'incidents' };
  if (state === 'success') return { state: 'success', history: 'trades' };
  if (state === 'incident') return { state: 'incident', history: 'incidents' };
  return { state, history: 'signals' };
}

test.describe('approved prediction execution workspace', () => {
  test('LLM hedge strategy switches in place and survives polling', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1100 });
    await openPrediction(page, 'threshold');
    const url = page.url();
    const strategyButtons = page.locator('[data-prediction-strategy]');
    await expect(strategyButtons).toHaveText(['YES/NO套利', 'LLM对冲套利']);
    await expect(page.locator('[data-prediction-strategy="yes_no"]')).toHaveAttribute('aria-pressed', 'true');
    await expect(page.locator('.pm-threshold-candidate')).toHaveCount(0);

    await page.getByRole('button', { name: 'LLM对冲套利', exact: true }).click();
    expect(page.url()).toBe(url);
    await expect(page.locator('[data-prediction-strategy="llm_hedge"]')).toHaveAttribute('aria-pressed', 'true');
    await expect(page.locator('.pm-threshold-candidate')).toHaveCount(2);

    const first = page.locator('[data-relation-key="threshold-approved"]');
    await expect(first).not.toHaveAttribute('open', '');
    await first.locator('summary').click();
    await expect(first).toHaveAttribute('open', '');
    await page.evaluate(() => (
      window as Window & { fetchPredictionState: () => Promise<void> }
    ).fetchPredictionState());
    await expect(page.locator('[data-prediction-strategy="llm_hedge"]')).toHaveAttribute('aria-pressed', 'true');
    await expect(first).toHaveAttribute('open', '');
  });

  test('LLM hedge strategy discloses current annualized math and rejection evidence responsively', async ({ page }) => {
    for (const viewport of [
      { width: 1440, height: 1100 },
      { width: 768, height: 1024 },
      { width: 375, height: 812 },
    ]) {
      await page.setViewportSize(viewport);
      await openPrediction(page, 'threshold');
      await page.getByRole('button', { name: 'LLM对冲套利', exact: true }).click();

      const approved = page.locator('[data-relation-key="threshold-approved"]');
      await expect(approved.locator('summary')).toContainText('21.5%');
      await expect(approved.locator('summary')).toContainText('$19.46 / $20.00');
      await approved.locator('summary').click();
      await expect(approved).toContainText('$0.54 / $19.46');
      await expect(approved).toContainText('21.55%');
      await expect(approved).toContainText('47 天');
      await expect(approved).toContainText('7 天');
      await expect(approved).toContainText('30 天');
      await expect(approved).toContainText('A · BUY NO');
      await expect(approved).toContainText('B · BUY YES');
      await expect(approved.locator('[data-action="participate"]')).toBeEnabled();

      const rejected = page.locator('[data-relation-key="threshold-rejected"]');
      await expect(rejected.locator('summary')).toContainText('REJECT');
      await rejected.locator('summary').click();
      await expect(rejected).toContainText('SPECIAL_SETTLEMENT_MISMATCH');
      await expect(rejected).toContainText('特殊结算可能破坏覆盖关系');
      await expect(rejected.locator('[data-action="participate"]')).toHaveCount(0);
      await expect(page.locator('.pm-scan-logs')).not.toHaveAttribute('open', '');
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    }
  });

  test('keeps the top navigation and workspace order exact', async ({ page }) => {
    await openPrediction(page, 'ready');
    await expect(page.locator('.dashboard-header')).toBeHidden();
    await expect(page.locator('.pm-nav button')).toHaveText(['持仓', '预测市场', '策略回测', '凯利实验室']);
    for (const label of ['交易钱包', '可用余额', '地区与连接', '实盘状态']) {
      await expect(page.locator('.pm-readiness')).toContainText(label);
    }
    await expect(page.locator('.pm-readiness-item')).toHaveCount(4);
    await expect(page.locator('.pm-readiness')).not.toContainText('首单验证');
    await expect(page.locator('.pm-metric')).toHaveCount(4);
    await expect(page.locator('.pm-metrics')).not.toContainText('WebSocket');
    await expect(page.locator('.pm-panel').nth(0)).toContainText('当前监控范围');
    await expect(page.locator('.pm-panel').nth(1)).toContainText('当前机会');
    await expect(page.locator('.pm-panel').nth(2)).toContainText('历史记录');
    await expect(page.locator('body')).toContainText('24h 成交量');
    await expect(page.locator('.pm-prototype-note')).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    expect(APPROVED_PROTOTYPE_SHA).toBe('12d3391');
    expect(PROTOTYPE_BASE_URL).toContain('prediction-market-truthful-ui-prototype.html');
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

  test('[UI-14] starts events collapsed and preserves the user choice across polling refreshes', async ({ page }) => {
    await openPrediction(page, 'ready');
    const firstEvent = page.locator('.pm-event').first();
    const refresh = () => page.evaluate(() => (
      window as Window & { fetchPredictionState: () => Promise<void> }
    ).fetchPredictionState());

    await expect(firstEvent).not.toHaveAttribute('open', '');
    await firstEvent.locator('summary').click();
    await expect(firstEvent).toHaveAttribute('open', '');
    await refresh();
    await expect(firstEvent).toHaveAttribute('open', '');

    await firstEvent.locator('summary').click();
    await expect(firstEvent).not.toHaveAttribute('open', '');
    await refresh();
    await expect(firstEvent).not.toHaveAttribute('open', '');
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
    await page.getByRole('button', { name: '重新检查并解除' }).click();
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
          health: { status: 'healthy', degraded_reasons: [] },
          readiness: { status: 'ready', balance: '50.00', geoblock: 'allowed', relayer: 'ready' },
          masked_wallet: '0x7A4E…91C2',
          policy_limits: { max_wallet_balance: '65', max_normal_cost: '20', max_emergency_loss: '2', min_estimated_profit: '1' },
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
            market_type: 'standard_binary', fee_status: 'fee_free',
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
    await expect(page.locator('.pm-readiness')).toContainText('不可用');
  });

  test('incident reset and history tabs remain visible', async ({ page }) => {
    await openPrediction(page, 'reset');
    await expect(page.locator('[data-action="open-reset"]')).toBeVisible();
    await page.locator('[data-action="open-reset"]').click();
    await expect(page.locator('.pm-modal')).toContainText('确认解除交易熔断');
    await page.getByRole('button', { name: '重新检查并解除' }).click();
    await expect(page.locator('[data-action="open-reset"]')).toHaveCount(0);
    for (const [kind, label] of [['signals', '信号历史'], ['executions', '交易与合并'], ['incidents', '事故']] as const) {
      await page.getByRole('button', { name: label, exact: true }).click();
      await expect(page.locator(`[data-history="${kind}"]`)).toHaveAttribute('aria-pressed', 'true');
    }
  });

  test('keeps the approved state-specific copy and six-column histories', async ({ page }) => {
    await openPrediction(page, 'incident');
    await expect(page.locator('.pm-alert.danger')).toContainText('交易已熔断');
    await expect(page.locator('.pm-alert.danger')).toContainText('YES 成交、NO 被拒');
    await expect(page.locator('.pm-alert.danger')).not.toContainText('$0.60');
    await expect(page.locator('.pm-alert.danger')).not.toContainText('macOS');
    await openPrediction(page, 'executing');
    for (const copy of ['双腿提交', '批次已提交', '成交核对']) {
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
    await expect(page.locator('.pm-event')).toHaveCount(0);
    await expect(page.locator('.pm-stack > .pm-panel').first().locator('.pm-empty')).toContainText('预测市场暂不可用');
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
    await expect(page.locator('.pm-readiness')).toHaveCSS('grid-template-columns', /.+ .+/);
    await expect(page.locator('.pm-metrics')).toHaveCSS('grid-template-columns', /.+ .+/);
    for (const height of await page.locator('#prediction-market-workspace').evaluate((root) => Array.from(root.querySelectorAll('button')).filter((element) => element.getClientRects().length).map((element) => element.getBoundingClientRect().height))) {
      expect(height).toBeGreaterThanOrEqual(44);
    }
  });

  test('keeps the approved A hierarchy at 1920 and 768 pixels', async ({ page }) => {
    for (const viewport of [{ width: 1920, height: 1200 }, { width: 768, height: 1024 }]) {
      await page.setViewportSize(viewport);
      await openPrediction(page, 'ready');
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
      await expect.poll(() => page.locator('.pm-readiness').evaluate((node) => getComputedStyle(node).gridTemplateColumns.split(' ').length)).toBe(4);
      await expect.poll(() => page.locator('.pm-metrics').evaluate((node) => getComputedStyle(node).gridTemplateColumns.split(' ').length)).toBe(4);
      await expect.poll(() => page.locator('.pm-layout').evaluate((node) => getComputedStyle(node).gridTemplateColumns.split(' ').length)).toBe(viewport.width === 1920 ? 2 : 1);
      await expect(page.locator('.pm-volume').first()).toContainText('24h 成交量');
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
    await expect(page.locator('.pm-opportunity button')).toHaveText('不可用');
  });

  test('[UI-05] cost disclosure', async ({ page }) => {
    await openPrediction(page, 'confirmation');
    await page.locator('[data-action="participate"]').click();
    await expect(page.locator('.pm-modal')).toContainText('$65');
    await expect(page.locator('.pm-modal')).toContainText('$20');
    await expect(page.locator('.pm-modal')).toContainText('$2');
  });

  test('missing and unknown data remains visible but cannot authorize an order', async ({ page }) => {
    await openPrediction(page, 'incomplete');
    await expect(page.locator('.pm-opportunity')).toContainText('数据不完整');
    await expect(page.locator('.pm-opportunity')).toContainText('-');
    await expect(page.locator('[data-action="participate"]')).toBeDisabled();

    await openPrediction(page, 'unknown');
    await expect(page.locator('.pm-status-line')).toContainText('Watcher 不可用');
    await expect(page.locator('.pm-metric strong')).toHaveText(['-', '-', '-', '-']);
    await expect(page.locator('.pm-readiness')).toContainText('不可用');
  });

  test('an incomplete latest preview never opens a confirmation or submits', async ({ page }) => {
    await openPrediction(page, 'preview-incomplete');
    const executionRequests: string[] = [];
    page.on('request', (request) => {
      if (request.url().includes('/prediction-arbitrage/executions')) executionRequests.push(request.method());
    });
    await page.locator('[data-action="participate"]').click();
    await expect(page.locator('.pm-modal')).toHaveCount(0);
    await expect(page.getByRole('alert')).toContainText('预览数据不完整，未下单');
    expect(executionRequests).toEqual([]);
  });

  test('incomplete completed-trade and incident states never grow sample facts', async ({ page }) => {
    await openPrediction(page, 'success-incomplete');
    await expect(page.locator('.pm-alert.success')).toContainText('交易已完成，详情数据未返回');
    for (const copy of ['$18.80', '$20.00', '+$1.20', '14:36:12']) {
      await expect(page.locator('.pm-alert.success')).not.toContainText(copy);
    }

    await openPrediction(page, 'incident-incomplete');
    await expect(page.locator('.pm-alert.danger')).toContainText('事故详情未返回');
    await page.locator('[data-action="open-reset"]').click();
    await expect(page.locator('.pm-modal')).toContainText('事故详情未返回');
    await expect(page.locator('.pm-modal')).toContainText('实际损失');
    await expect(page.locator('.pm-modal')).not.toContainText('$0.60');
    await expect(page.locator('.pm-modal')).not.toContainText('未完成订单 0');
  });

  test('reset dialog shows only stored incident facts before live recheck', async ({ page }) => {
    await openPrediction(page, 'incident');
    await page.locator('[data-action="open-reset"]').click();
    for (const copy of ['事故时间', '市场', 'YES 成交、NO 被拒', '-$0.60', '重新检查并解除']) {
      await expect(page.locator('.pm-modal')).toContainText(copy);
    }
    await expect(page.locator('.pm-modal')).not.toContainText('未完成订单 0');
    await expect(page.locator('.pm-modal')).not.toContainText('通知状态');
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
          for (const height of await page.locator('#prediction-market-workspace').evaluate((root) => Array.from(root.querySelectorAll('button')).filter((element) => element.getClientRects().length).map((element) => element.getBoundingClientRect().height))) {
            expect(height).toBeGreaterThanOrEqual(44);
          }
        }
      });
    }
  }

});
