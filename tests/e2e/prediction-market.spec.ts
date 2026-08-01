import { expect, test, type Page } from '@playwright/test';

async function openPrediction(page: Page, state = 'ready') {
  await page.goto(`/?prediction_state=${state}`, { waitUntil: 'networkidle' });
  await page.getByRole('button', { name: '预测市场', exact: true }).click();
  await expect(page.locator('#prediction-market-workspace')).toBeVisible();
  await expect(page.getByRole('heading', { name: '预测市场套利' })).toBeVisible();
}

test.describe('YES/NO arbitrage signal workspace', () => {
  test('keeps the original two-column hierarchy and approved signal columns', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1100 });
    await openPrediction(page);
    await expect(page.locator('.pm-layout > .pm-panel').first()).toContainText('当前监控范围');
    await expect(page.locator('[data-prediction-history-panel]')).toContainText('套利信号');
    await expect(page.locator('[data-prediction-history-panel] .pm-table th')).toHaveText([
      '出现时间（HKT）', '标的', '持续', '触发时利润', '实时利润', '通知', '操作',
    ]);
    await expect(page.locator('body')).not.toContainText('当前机会');
    await expect(page.locator('body')).not.toContainText('仅监控');
    await expect(page.locator('.pm-page-head')).toContainText('Watcher 数据时间');
    await expect(page.locator('[data-prediction-history-panel]')).toContainText('信号刷新时间');
    await expect(page.locator('.pm-title-zh').first()).toContainText('以色列与伊朗停火');
    await expect(page.locator('.pm-title-en').first()).toContainText('Will the Israel-Iran ceasefire');
  });

  test('preserves the production LLM hedge math and rejection evidence', async ({ page }) => {
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
      await expect(page.locator('.pm-scan-logs[open]')).toHaveCount(0);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    }
  });

  test('keeps the production navigation and workspace order', async ({ page }) => {
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
    await expect(page.locator('.pm-layout > .pm-panel').first()).toContainText('当前监控范围');
    await expect(page.locator('[data-prediction-history-panel]')).toContainText('套利信号');
    await expect(page.locator('body')).not.toContainText('当前机会');
    await expect(page.locator('.pm-volume').first()).toContainText('24h 成交量');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });

  test('preserves keyboard focus and cost disclosures in the manual confirmation modal', async ({ page }) => {
    await openPrediction(page, 'confirmation');
    const trigger = page.locator('[data-action="participate"]').first();
    await trigger.click();
    await expect(page.locator('.pm-modal')).toBeVisible();
    for (const copy of ['确认真实下单', '$20', '$2', '免手续费']) {
      await expect(page.locator('.pm-modal')).toContainText(copy);
    }
    await page.keyboard.press('Escape');
    await expect(page.locator('.pm-modal')).toHaveCount(0);
    await expect(trigger).toBeFocused();
  });

  test('replaces only the signal panel and does not auto-switch history tabs', async ({ page }) => {
    await openPrediction(page);
    const firstEvent = page.locator('.pm-event').first();
    await firstEvent.locator('summary').click();
    const panel = page.locator('[data-prediction-history-panel]');
    await page.evaluate(() => (window as Window & { stopPredictionPolling: () => void }).stopPredictionPolling());
    await page.evaluate(() => (window as Window & { stopPredictionSignalPolling: () => void }).stopPredictionSignalPolling());
    await page.getByRole('button', { name: '交易与合并', exact: true }).click();
    await expect(page.locator('[data-history="executions"]')).toHaveAttribute('aria-pressed', 'true');
    await page.evaluate(() => {
      (window as Window & { __signalUiIdentity?: { scope: Element | null; event: Element | null } }).__signalUiIdentity = {
        scope: document.querySelector('.pm-event-list'),
        event: document.querySelector('.pm-event'),
      };
    });
    await page.evaluate(() => (
      window as Window & { loadPredictionHistory: (kind: string, options?: { panelOnly?: boolean }) => Promise<void> }
    ).loadPredictionHistory('signals', { panelOnly: true }));
    await expect(page.locator('[data-history="executions"]')).toHaveAttribute('aria-pressed', 'true');
    expect(await page.evaluate(() => {
      const identity = (window as Window & { __signalUiIdentity?: { scope: Element | null; event: Element | null } }).__signalUiIdentity;
      return identity?.scope === document.querySelector('.pm-event-list') && identity?.event === document.querySelector('.pm-event');
    })).toBe(true);
    await expect(panel).toHaveCount(1);
  });

  test('closed signals remove the operation button and show a dash for live profit', async ({ page }) => {
    await openPrediction(page, 'signal-closed');
    const firstRow = page.locator('[data-prediction-history-panel] tbody tr').first();
    await expect(firstRow.locator('[data-label="操作"]')).toHaveText('');
    await expect(firstRow.locator('[data-label="实时利润"]')).toContainText('—');
    await expect(firstRow.locator('[data-action="participate"]')).toHaveCount(0);
  });

  test('failed signal refresh freezes the clock and suppresses operations', async ({ page }) => {
    await openPrediction(page);
    const signalClock = page.locator('[data-prediction-history-panel] .pm-clock > span');
    await expect(signalClock).toHaveText(/信号刷新时间：\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} HKT/);
    const successfulClockText = await signalClock.innerText();
    await page.evaluate(() => (window as Window & { stopPredictionSignalPolling: () => void }).stopPredictionSignalPolling());
    await page.route('**/api/prediction-arbitrage/history?kind=signals**', async (route) => {
      await route.abort();
    });
    await page.evaluate(() => (
      window as Window & { loadPredictionHistory: (kind: string, options?: { panelOnly?: boolean }) => Promise<void> }
    ).loadPredictionHistory('signals', { panelOnly: true }));
    await expect(page.locator('.pm-clock-danger')).toBeVisible();
    await expect(signalClock).toHaveText(successfulClockText);
    await expect(page.locator('[data-prediction-history-panel] [data-action="participate"]')).toHaveCount(0);
    await expect(page.locator('[data-prediction-history-panel] tbody tr')).not.toHaveCount(0);
  });

  test('重新检查 keeps the existing preview then confirmation flow', async ({ page }) => {
    await openPrediction(page);
    const previewRequests: string[] = [];
    const confirmRequests: string[] = [];
    page.on('request', (request) => {
      if (request.url().includes('/prediction-arbitrage/preview')) previewRequests.push(request.method());
      if (request.url().includes('/prediction-arbitrage/executions')) confirmRequests.push(request.method());
    });
    const trigger = page.locator('[data-action="participate"]').first();
    await trigger.click();
    await expect(page.locator('.pm-modal')).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(page.locator('.pm-modal')).toHaveCount(0);
    await trigger.click();
    await page.getByRole('button', { name: /确认下单/ }).click();
    await expect.poll(() => confirmRequests.length).toBe(1);
    expect(previewRequests).toEqual(['POST', 'POST']);
  });

  test('desktop and mobile layouts do not overflow and retain 44px controls', async ({ page }) => {
    for (const viewport of [{ width: 1440, height: 1100 }, { width: 375, height: 812 }]) {
      await page.setViewportSize(viewport);
      await openPrediction(page);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
      for (const height of await page.locator('#prediction-market-workspace').evaluate((root) => Array.from(root.querySelectorAll('button')).filter((element) => element.getClientRects().length).map((element) => element.getBoundingClientRect().height))) {
        expect(height).toBeGreaterThanOrEqual(44);
      }
    }
  });

  test('LLM hedge remains unchanged and switching strategy stops signal-only polling', async ({ page }) => {
    await openPrediction(page, 'threshold');
    await expect(page.locator('[data-prediction-strategy="yes_no"]')).toHaveAttribute('aria-pressed', 'true');
    await page.getByRole('button', { name: 'LLM对冲套利', exact: true }).click();
    await expect(page.locator('[data-prediction-strategy="llm_hedge"]')).toHaveAttribute('aria-pressed', 'true');
    await expect(page.locator('.pm-threshold-candidate')).toHaveCount(2);
    await expect(page.locator('.pm-threshold-candidate').first()).toContainText('21.5%');
    await expect(page.locator('.pm-threshold-candidate').first()).not.toContainText('仅监控');
    await page.evaluate(() => (
      window as Window & { startPredictionSignalPolling: () => void }
    ).startPredictionSignalPolling());
    await expect(page.locator('.pm-threshold-candidate')).toHaveCount(2);
  });

  test('state-specific alerts remain truthful without neutral monitoring labels', async ({ page }) => {
    await openPrediction(page, 'incident');
    await expect(page.locator('.pm-alert.danger')).toContainText('交易已熔断');
    await openPrediction(page, 'degraded');
    await expect(page.locator('.pm-alert.danger')).toContainText('数据连接异常');
    await expect(page.locator('body')).not.toContainText('仅监控');
  });

  test('keeps loading, unavailable, and unknown states fail-closed', async ({ page }) => {
    await openPrediction(page, 'loading');
    await expect(page.locator('.pm-event')).toHaveCount(0);
    await expect(page.locator('[data-prediction-history-panel] tbody tr')).toHaveCount(3);
    await expect(page.locator('[data-prediction-history-panel] [data-action="participate"]')).toHaveCount(0);
    await expect(page.locator('.pm-readiness')).toContainText('不可用');

    await openPrediction(page, 'unavailable');
    await expect(page.locator('.pm-status-line')).toContainText('Watcher 不可用');
    await expect(page.locator('.pm-metric strong')).toHaveText(['-', '-', '-', '-']);
    await expect(page.locator('.pm-readiness')).toContainText('不可用');
    await expect(page.locator('[data-prediction-history-panel] [data-action="participate"]')).toHaveCount(0);

    await openPrediction(page, 'unknown');
    await expect(page.locator('.pm-status-line')).toContainText('Watcher 不可用');
    await expect(page.locator('.pm-metric strong')).toHaveText(['-', '-', '-', '-']);
    await expect(page.locator('.pm-readiness')).toContainText('不可用');
  });

  test('keeps full execution progress and success facts', async ({ page }) => {
    await openPrediction(page, 'executing');
    for (const copy of ['双腿提交', '批次已提交', '成交核对']) {
      await expect(page.locator('.pm-progress')).toContainText(copy);
    }
    await openPrediction(page, 'success');
    for (const copy of ['实际成本 $18.80', '合并收回 $20.00', '+$1.20', '14:36:12']) {
      await expect(page.locator('.pm-alert.success')).toContainText(copy);
    }
  });

  test('orders monitoring events by actionability and exposes ranking volume', async ({ page }) => {
    await openPrediction(page);
    await expect(page.locator('.pm-title-zh')).toHaveText([
      '以色列与伊朗停火是否持续至 8 月 31 日？',
      '以色列与伊朗停火是否持续至 2026 年 8 月 31 日？',
      '2026 年 9 月美联储是否降息？',
      '以太坊会在 9 月前突破 $6,000？',
    ]);
    await expect(page.locator('.pm-event-title')).toContainText([
      '以色列与伊朗停火是否持续至 8 月 31 日？Will the Israel-Iran ceasefire continue through August 31, 2026?',
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

  test('preserves expanded and collapsed monitoring event choices across refreshes', async ({ page }) => {
    await openPrediction(page, 'ready');
    const firstEvent = page.locator('.pm-event').first();
    const refresh = () => page.evaluate(() => (
      window as Window & { fetchPredictionState: () => Promise<void> }
    ).fetchPredictionState());
    await expect(firstEvent).not.toHaveAttribute('open', '');
    await firstEvent.locator('summary').click();
    await expect(firstEvent).toHaveAttribute('open', '');
    await refresh();
    await expect(page.locator('.pm-event').first()).toHaveAttribute('open', '');
    await page.locator('.pm-event').first().locator('summary').click();
    await expect(page.locator('.pm-event').first()).not.toHaveAttribute('open', '');
    await refresh();
    await expect(page.locator('.pm-event').first()).not.toHaveAttribute('open', '');
  });

  test('preserves expanded monitoring events across the five-second state refresh', async ({ page }) => {
    await openPrediction(page);
    const firstEvent = page.locator('.pm-event').first();
    await firstEvent.locator('summary').click();
    await page.evaluate(() => (window as Window & { fetchPredictionState: () => Promise<void> }).fetchPredictionState());
    await expect(page.locator('.pm-event').first()).toHaveAttribute('open', '');
  });

  test('preview rejection never opens a modal or submits an execution', async ({ page }) => {
    await openPrediction(page, 'preview-rejected');
    const executionRequests: string[] = [];
    page.on('request', (request) => {
      if (request.url().includes('/prediction-arbitrage/executions')) executionRequests.push(request.method());
    });
    await page.locator('[data-action="participate"]').first().click();
    await expect(page.locator('.pm-modal')).toHaveCount(0);
    await expect(page.getByRole('alert')).toContainText('机会已变化或已失效');
    expect(executionRequests).toEqual([]);
  });

  test('reset denial leaves the circuit-breaker modal open', async ({ page }) => {
    await openPrediction(page, 'reset-denied');
    await page.locator('[data-action="open-reset"]').click();
    await page.getByRole('button', { name: '重新检查并解除' }).click();
    await expect(page.locator('.pm-modal')).toBeVisible();
    await expect(page.getByRole('alert').filter({ hasText: '本次操作未提交' })).toBeVisible();
  });

  test('maps live state field aliases without inventing execution facts', async ({ page }) => {
    await page.route('**/api/prediction-arbitrage/state*', async (route) => {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          status: 'healthy', health: { status: 'healthy', degraded_reasons: [] },
          readiness: { status: 'ready', balance: '50.00', geoblock: 'allowed', relayer: 'ready' },
          masked_wallet: '0x7A4E…91C2',
          policy_limits: { max_wallet_balance: '65', max_normal_cost: '20', max_emergency_loss: '2', min_estimated_profit: '1' },
          event_count: 1, market_count: 1, token_count: 2, signals_24h: 1,
          events: [{ event_id: 'live-event', question: '真实问题字段', volume_24h: 1234567, markets: [{ question: '真实市场字段', actionable: true }] }],
          opportunities: [{ opportunity_id: 'live-opp', question: '真实问题字段', actionable: true, market_type: 'standard_binary', fee_status: 'fee_free', yes_max_price: '0.401', no_max_price: '0.499', yes_max_cost: '8.02', no_max_cost: '9.98', total_max_cost: '18.00', minimum_profit: '2.00', quantity: '20' }],
          current_execution: { state: 'submitting', question: '真实问题字段', execution_id: 'live-exec' },
          histories: { signals: [] }, breaker: { open: false }, csrf_token: 'fixture-csrf',
        }),
      });
    });
    await openPrediction(page);
    await expect(page.locator('.pm-event-title')).toHaveText(['真实问题字段']);
    await expect(page.locator('.pm-progress')).toBeVisible();
    await expect(page.locator('.pm-readiness')).toContainText('不可用');
  });

  test('incomplete preview remains closed and does not submit', async ({ page }) => {
    await openPrediction(page, 'preview-incomplete');
    const executionRequests: string[] = [];
    page.on('request', (request) => {
      if (request.url().includes('/prediction-arbitrage/executions')) executionRequests.push(request.method());
    });
    await page.locator('[data-action="participate"]').first().click();
    await expect(page.locator('.pm-modal')).toHaveCount(0);
    await expect(page.getByRole('alert')).toContainText('预览数据不完整，未下单');
    expect(executionRequests).toEqual([]);
  });

  test('incident reset and history tabs stay available', async ({ page }) => {
    await openPrediction(page, 'incident');
    await expect(page.locator('[data-action="open-reset"]')).toBeVisible();
    await page.locator('[data-action="open-reset"]').click();
    await expect(page.locator('.pm-modal')).toContainText('确认解除交易熔断');
    await page.getByRole('button', { name: '重新检查并解除' }).click();
    await expect(page.locator('[data-action="open-reset"]')).toHaveCount(0);
    for (const [kind, label] of [['signals', '套利信号'], ['executions', '交易与合并'], ['incidents', '事故']] as const) {
      await page.getByRole('button', { name: label, exact: true }).click();
      await expect(page.locator(`[data-history="${kind}"]`)).toHaveAttribute('aria-pressed', 'true');
    }
  });

  test('stored incident facts are shown before reset recheck', async ({ page }) => {
    await openPrediction(page, 'incident');
    await page.locator('[data-action="open-reset"]').click();
    for (const copy of ['事故时间', '市场', 'YES 成交、NO 被拒', '-$0.60', '重新检查并解除']) {
      await expect(page.locator('.pm-modal')).toContainText(copy);
    }
    await expect(page.locator('.pm-modal')).not.toContainText('未完成订单 0');
  });

  test('incomplete execution and incident states do not invent sample facts', async ({ page }) => {
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

  test('retains the approved desktop and tablet responsive grid', async ({ page }) => {
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

  test('history tabs retain their selected state', async ({ page }) => {
    await openPrediction(page);
    for (const [kind, label] of [['signals', '套利信号'], ['executions', '交易与合并'], ['incidents', '事故']] as const) {
      await page.getByRole('button', { name: label, exact: true }).click();
      await expect(page.locator(`[data-history="${kind}"]`)).toHaveAttribute('aria-pressed', 'true');
    }
  });
});
