import { expect, test, type Page } from '@playwright/test';

async function openPrediction(page: Page, state = 'ready') {
  await page.goto(`/?prediction_state=${state}`, { waitUntil: 'networkidle' });
  await page.getByRole('button', { name: '预测市场', exact: true }).click();
  await expect(page.locator('#prediction-market-workspace')).toBeVisible();
  await expect(page.getByRole('heading', { name: '预测市场套利' })).toBeVisible();
}

test.describe('YES/NO arbitrage signal workspace', () => {
  test('shows approved signal columns without the monitoring-scope panel', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1100 });
    await openPrediction(page);
    await expect(page.locator('body')).not.toContainText('当前监控范围');
    await expect(page.locator('[data-prediction-history-panel]')).toContainText('套利信号');
    await expect(page.locator('[data-prediction-history-panel] .pm-table th')).toHaveText([
      '出现时间（HKT）', '标的', '24h 成交量', '资金占用', '净回报', '操作',
    ]);
    await expect(page.locator('body')).not.toContainText('当前机会');
    await expect(page.locator('body')).not.toContainText('仅监控');
    await expect(page.locator('.pm-page-head')).toContainText('Watcher 数据时间');
    await expect(page.locator('[data-prediction-history-panel]')).toContainText('信号刷新时间');
    await expect(page.locator('.pm-title-zh').first()).toContainText('以色列与伊朗停火');
    await expect(page.locator('.pm-title-en').first()).toContainText('Will the Israel-Iran ceasefire');
    const signalTitle = page.locator('.pm-title-cell').first();
    await expect(signalTitle.locator('.pm-title-en')).toHaveCSS('font-weight', '800');
    await expect(signalTitle.locator('.pm-title-zh')).toHaveCSS('font-size', '12px');
    const targetRow = page.locator('[data-prediction-history-panel] tbody tr').filter({ hasText: 'Will Bitcoin be above $90,000 on December 31, 2026?' });
    await expect(targetRow).toContainText('Will Bitcoin be above $90,000 on December 31, 2026? / Will Bitcoin be above $100,000 on December 31, 2026?');
    await expect(targetRow).toContainText('比特币在 12 月 31 日是否高于 9 万美元？ / 比特币在 12 月 31 日是否高于 10 万美元？');
  });

  test('renders shared venue truth and protects the cross-venue confirmation flow', async ({ page }) => {
    for (const viewport of [{ width: 1440, height: 1100 }, { width: 375, height: 812 }]) {
      await page.setViewportSize(viewport);
      await openPrediction(page, 'cross-manual-confirm');
      const venueHeader = page.locator('.pm-venue-readiness');
      const tabs = page.locator('.pm-strategy-tabs');
      await expect(venueHeader).toBeVisible();
      expect(await venueHeader.evaluate((header, strategyTabs) => Boolean(
        header.compareDocumentPosition(strategyTabs) & Node.DOCUMENT_POSITION_FOLLOWING,
      ), await tabs.elementHandle())).toBe(true);
      await expect(page.locator('.pm-venue-card')).toHaveCount(2);
      const polymarket = page.locator('.pm-venue-card').filter({ hasText: 'Polymarket' });
      for (const text of ['REST：ready', 'WebSocket：ready', '0x7A4E…91C2', '50.00 pUSD', '可以交易', '最近成功 2026-08-02T01:00:00Z']) {
        await expect(polymarket).toContainText(text);
      }
      const predict = page.locator('.pm-venue-card').filter({ hasText: 'Predict.fun' });
      for (const text of ['REST：ready', 'WebSocket：ready', '0xcE23…f435', '12.34 USDT', '可以交易', '最近成功 2026-08-02T00:59:58Z']) {
        await expect(predict).toContainText(text);
      }
      await expect(venueHeader).not.toContainText('62.34');
      await expect(page.locator('.pm-metrics')).toHaveCount(0);
      await page.getByRole('button', { name: 'LLM对冲套利', exact: true }).click();
      await expect(page.locator('.pm-venue-readiness')).toContainText('Predict.fun');
      await page.getByRole('button', { name: 'YES/NO套利', exact: true }).click();

      const funnel = page.locator('.pm-cross-venue-funnel');
      await expect(funnel.locator('.pm-funnel-stage > span')).toHaveText([
        '正在监视', '正收益', '年化达标', '已提交',
      ]);
      await expect(funnel).toContainText('Codex 认为可以');
      await expect(funnel).toContainText('文字一致');
      const manualCard = page.locator('.pm-manual-card');
      await expect(manualCard).toHaveCount(1);
      await expect(manualCard).toContainText('人工下单');
      await expect(manualCard).toContainText('结算规则可能不一致');
      await expect(manualCard).toContainText('20.10%');
      await expect(manualCard).not.toContainText('年化低于 15% 入场门槛');
      const previewRequests: string[] = [];
      const confirmRequests: string[] = [];
      page.on('request', (request) => {
        if (request.url().includes('/prediction-arbitrage/preview')) previewRequests.push(request.method());
        if (request.url().includes('/prediction-arbitrage/executions')) confirmRequests.push(request.method());
      });
      const trigger = manualCard.getByRole('button', { name: '人工下单' });
      await trigger.click();
      expect(confirmRequests).toEqual([]);
      await expect(page.locator('.pm-modal')).toBeVisible();
      for (const text of [
        'Predict.fun · BUY YES', 'Polymarket · BUY NO', '最高 $0.470', '最高 $0.490',
        '净可兑付份额', 'USDT', 'pUSD', '含费最大成本', '最低赔付', '最低净利润',
        '简单年化', '统一结算截止', 'Codex APPROVE', '可用余额', '待结算占用',
        '$20.00', '$2.00', '自动兑付', '不是原子交易',
      ]) {
        await expect(page.locator('.pm-modal')).toContainText(text);
      }
      if (viewport.width === 375) {
        await expect(page.locator('.pm-modal-actions')).toHaveCSS('position', 'sticky');
      }
      await page.keyboard.press('Escape');
      await expect(page.locator('.pm-modal')).toHaveCount(0);
      await expect(trigger).toBeFocused();
      await trigger.click();
      await page.getByRole('button', { name: /确认下单/ }).dblclick();
      await expect.poll(() => confirmRequests.length).toBe(1);
      expect(previewRequests).toEqual(['POST', 'POST']);
      await expect(page.locator('.pm-venue-card').nth(1)).toHaveCSS('border-right-width', '1px');
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    }
  });

  test('covers final allowance gas and scan fixture states on desktop and mobile', async ({ page }) => {
    const cases: Array<[string, string[]]> = [
      ['ready-zero-allowance', ['Predict Account', '授权 $0.00 USDT', '可以交易', 'Privy signer', 'BNB']],
      ['signer-bnb-low', ['Privy signer', '只读', '当前 0.001 BNB', '需要 0.004 BNB', '最低保留 0.006 BNB', 'BNB top-up to 0xBnbSigner…BEEF']],
      ['cross-signal-bnb-low', ['年化达标', '只读', 'BNB top-up to 0xBnbSigner…BEEF']],
      ['residual-allowance', ['熔断只读', '残余授权', '$2.40 USDT', '清理残余授权']],
      ['cleanup-success', ['熔断只读', '清理残余授权']],
      ['cleanup-failure', ['熔断只读', '清理残余授权']],
      ['cross-stale-stage4', ['正在监视', '正收益', '年化达标', '已提交', '保留时间 2026-08-03T15:39:00Z']],
      ['cross-empty-scan', ['当前没有合格跨所市场', '扫描正常，没有失败']],
      ['first-canary-cap5', ['首单验证', '单笔成本上限 $5.00']],
      ['completed-canary-cap20', ['常规上限', '单笔成本上限 $20.00']],
      ['post-approval-cleared', ['未下单 · 授权已清零']],
      ['cross-grouped-history', ['授权', '0xapprove-fixture', '双腿订单', '0xorders-fixture', '对账', '0xreconcile-fixture', '授权清零', '0xcleanup-fixture']],
    ];
    for (const viewport of [{ width: 1440, height: 1100 }, { width: 375, height: 812 }]) {
      await page.setViewportSize(viewport);
      for (const [state, texts] of cases) {
        await openPrediction(page, state);
        if (state === 'cross-grouped-history') {
          await page.getByRole('button', { name: '交易与合并', exact: true }).click();
        }
        for (const text of texts) await expect(page.locator('body')).toContainText(text);
        await expect(page.locator('body')).not.toContainText('倒计时');
        const smallButtons = await page.locator('button:visible').evaluateAll((buttons) =>
          buttons.map((button) => (button as HTMLElement).getBoundingClientRect().height).filter((height) => height < 44),
        );
        expect(smallButtons).toEqual([]);
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
      }
    }
  });

  test('cleans residual Predict allowance only after a second confirmation', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 });
    await openPrediction(page, 'residual-allowance');
    const cleanupRequests: string[] = [];
    page.on('request', async (request) => {
      if (request.url().includes('/prediction-arbitrage/predict-allowance/cleanup')) {
        cleanupRequests.push(request.postData() || '');
      }
    });
    const trigger = page.getByRole('button', { name: '清理残余授权' });
    await trigger.click();
    await expect(page.locator('.pm-modal')).toContainText('不转移 USDT');
    await expect(page.locator('.pm-modal')).toContainText('$2.40 → $0.00');
    await expect(page.locator('.pm-modal')).toContainText('0xcE23…f435');
    await expect(page.locator('.pm-modal')).toContainText('0xSpender…C0DE');
    expect(cleanupRequests).toEqual([]);
    await page.getByRole('button', { name: /我知道/ }).click();
    expect(cleanupRequests).toEqual([]);
    await page.getByRole('button', { name: /二次确认/ }).click();
    await expect.poll(() => cleanupRequests).toEqual(['{"confirm":true}']);
  });

  test('keeps observe-only cross signals in history without an execution action', async ({ page }) => {
    await openPrediction(page, 'cross-observe-only');
    await expect(page.locator('.pm-manual-card')).toHaveCount(0);
    await expect(page.locator('.pm-cross-venue-funnel')).toContainText('年化达标');

    const history = page.locator('[data-prediction-history-panel]');
    const observeSignal = history.locator('tbody tr').filter({ hasText: '跨所只观察信号' });
    await expect(observeSignal).toContainText('仅观察');
    await expect(observeSignal).toContainText('只观察模式');
    await expect(observeSignal.locator('[data-action="participate"]')).toHaveCount(0);
  });

  test('keeps invalid-cutoff cross candidates out of the orderable list', async ({ page }) => {
    let cutoff = '2099-12-31';
    await page.route('**/api/prediction-arbitrage/state*', async (route) => {
      const response = await route.fetch();
      const payload = await response.json();
      for (const opportunity of payload.opportunities ?? []) {
        if (opportunity.market_type === 'cross_venue_yes_no') opportunity.canonical_cutoff = cutoff;
      }
      await route.fulfill({ response, body: JSON.stringify(payload) });
    });

    for (const invalidCutoff of [
      '2099-12-31',
      '2099-12-31T23:59:00',
      '2099-12-31T23:59:00+08:00',
      '2099-02-30T23:59:00Z',
      '2100-02-29T23:59:00Z',
      '2020-01-01T00:00:00Z',
    ]) {
      cutoff = invalidCutoff;
      await openPrediction(page, 'cross-manual-confirm');
      await expect(page.locator('.pm-manual-card')).toHaveCount(0);
      await expect(page.locator('.pm-cross-venue-funnel')).toContainText('年化达标');
    }
  });

  test('history follows the live cross execution mode instead of stale stored mode', async ({ page }) => {
    await openPrediction(page, 'cross-history-stale-manual');
    const history = page.locator('[data-prediction-history-panel]');
    const staleSignal = history.locator('tbody tr').filter({ hasText: '历史陈旧手动信号' });
    await expect(staleSignal).toContainText('仅观察');
    await expect(staleSignal).toContainText('只观察模式');
    await expect(staleSignal.locator('[data-action="participate"]')).toHaveCount(0);
  });

  test('renders cross execution history, holding, dust, breaker, and redemption states', async ({ page }) => {
    await openPrediction(page, 'cross-submitting');
    await expect(page.locator('.pm-progress')).toContainText('正在提交');
    await expect(page.locator('.pm-progress')).toContainText('分别结算/自动兑付');
    await expect(page.locator('.pm-progress')).not.toContainText('自动合并');
    await openPrediction(page, 'cross-reconciling');
    await expect(page.locator('.pm-progress')).toContainText('正在读取两腿结果');
    await expect(page.locator('.pm-progress')).toContainText('分别结算/自动兑付');
    await expect(page.locator('.pm-progress')).not.toContainText('自动合并');
    await openPrediction(page, 'cross-holding');
    await expect(page.locator('.pm-alert')).toContainText('待兑付');
    await expect(page.locator('.pm-alert')).not.toContainText('自动合并');
    await page.getByRole('button', { name: '交易与合并', exact: true }).click();
    const executions = page.locator('[data-prediction-history-panel]');
    for (const text of ['Predict.fun · YES', 'Polymarket · NO', 'submitting', 'reconciling', '待兑付']) {
      await expect(executions).toContainText(text);
    }
    await page.getByRole('button', { name: '事故', exact: true }).click();
    const incidents = page.locator('[data-prediction-history-panel]');
    for (const text of ['dust incident', '跨所熔断', 'Predict.fun · YES', 'Polymarket · NO']) {
      await expect(incidents).toContainText(text);
    }
  });

  test('shows a venue reason instead of its old success timestamp', async ({ page }) => {
    await openPrediction(page, 'predict-pending');
    const predict = page.locator('.pm-venue-card').filter({ hasText: 'Predict.fun' });
    await expect(predict).toContainText('原因：Predict API Key 待分配');
    await expect(predict).not.toContainText('最近成功');
  });

  test('localizes a Predict construction failure in its venue card', async ({ page }) => {
    await openPrediction(page, 'predict-degraded');
    const predict = page.locator('.pm-venue-card').filter({ hasText: 'Predict.fun' });
    await expect(predict).toContainText('原因：Predict.fun 监控初始化失败');
    await expect(predict).not.toContainText('predict construction failed');
    await expect(predict).not.toContainText('最近成功');
  });

  test('localizes remaining Predict and cross-venue reasons in venue cards', async ({ page }) => {
    for (const [scenario, label, rawReason] of [
      ['predict-not-configured', 'Predict.fun 尚未配置', 'predict not configured'],
      ['cross-venue-unavailable', '跨交易所监控暂不可用', 'cross venue unavailable'],
      ['predict-stale', 'Predict.fun 数据已过期', 'predict stale'],
      ['predict-auth-blocked', 'Predict.fun API Key 认证受阻', 'predict auth blocked'],
    ]) {
      await openPrediction(page, scenario);
      const predict = page.locator('.pm-venue-card').filter({ hasText: 'Predict.fun' });
      await expect(predict).toContainText(`原因：${label}`);
      await expect(predict).not.toContainText(rawReason);
      await expect(predict).not.toContainText('最近成功');
    }
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

      const approved = page.locator('.pm-candidate-table tbody tr[data-relation-key="threshold-approved"]');
      await expect(approved).toContainText('21.55%');
      await expect(approved).toContainText('47 天');
      await expect(approved).toContainText('2000');
      await expect(approved.locator('[data-action="participate"]')).toBeEnabled();

      const rejected = page.locator('.pm-candidate-table tbody tr[data-relation-key="threshold-rejected"]');
      await expect(rejected).toContainText('仅观察');
      await expect(rejected).toContainText('SPECIAL_SETTLEMENT_MISMATCH');
      await expect(rejected.locator('[data-action="participate"]')).toHaveCount(0);
      await expect(page.locator('.pm-scan-logs[open]')).toHaveCount(0);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    }
  });

  test('keeps the production navigation and workspace order', async ({ page }) => {
    await openPrediction(page, 'ready');
    await expect(page.locator('.dashboard-header')).toBeHidden();
    await expect(page.locator('.pm-nav button')).toHaveText(['持仓', '预测市场', '策略回测', '凯利实验室']);
    for (const text of ['Polymarket', 'Predict.fun', 'REST：ready', 'WebSocket：ready']) {
      await expect(page.locator('.pm-venue-readiness')).toContainText(text);
    }
    await expect(page.locator('.pm-venue-card')).toHaveCount(2);
    await expect(page.locator('.pm-metrics')).toHaveCount(0);
    await expect(page.locator('body')).not.toContainText('当前监控范围');
    await expect(page.locator('[data-prediction-history-panel]')).toContainText('套利信号');
    await expect(page.locator('body')).not.toContainText('当前机会');
    await expect(page.locator('[data-prediction-history-panel]')).toContainText('24h 成交量');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });

  test('preserves keyboard focus and cost disclosures in the manual confirmation modal', async ({ page }) => {
    await openPrediction(page, 'confirmation');
    const trigger = page.locator('[data-action="participate"][data-opportunity-id="opp-ceasefire"]');
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
    const panel = page.locator('[data-prediction-history-panel]');
    await page.evaluate(() => (window as Window & { stopPredictionPolling: () => void }).stopPredictionPolling());
    await page.evaluate(() => (window as Window & { stopPredictionSignalPolling: () => void }).stopPredictionSignalPolling());
    await page.getByRole('button', { name: '交易与合并', exact: true }).click();
    await expect(page.locator('[data-history="executions"]')).toHaveAttribute('aria-pressed', 'true');
    await page.evaluate(() => (
      window as Window & { loadPredictionHistory: (kind: string, options?: { panelOnly?: boolean }) => Promise<void> }
    ).loadPredictionHistory('signals', { panelOnly: true }));
    await expect(page.locator('[data-history="executions"]')).toHaveAttribute('aria-pressed', 'true');
    await expect(panel).toHaveCount(1);
    await page.getByRole('button', { name: '套利信号', exact: true }).click();
    await expect(page.locator('[data-history="signals"]')).toHaveAttribute('aria-pressed', 'true');
    await expect(panel.locator('tbody tr')).toHaveCount(5);
  });

  test('closed signals remove the operation button and show a dash for live profit', async ({ page }) => {
    await openPrediction(page, 'signal-closed');
    const firstRow = page.locator('[data-prediction-history-panel] tbody tr').first();
    await expect(firstRow.locator('[data-label="操作"]')).toContainText('飞书已发');
    await expect(firstRow.locator('[data-label="净回报"]')).toContainText('—');
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
      const targetRow = page.locator('[data-prediction-history-panel] tbody tr').filter({ hasText: 'Will Bitcoin be above $90,000 on December 31, 2026?' });
      await expect(targetRow).toContainText('Will Bitcoin be above $90,000 on December 31, 2026? / Will Bitcoin be above $100,000 on December 31, 2026?');
      await expect(targetRow).toContainText('比特币在 12 月 31 日是否高于 9 万美元？ / 比特币在 12 月 31 日是否高于 10 万美元？');
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
    await expect(page.locator('.pm-candidate-table tbody tr')).toHaveCount(2);
    await expect(page.locator('.pm-candidate-table tbody tr').first()).toContainText('21.55%');
    await expect(page.locator('.pm-candidate-table tbody tr').first()).not.toContainText('仅监控');
    await expect(page.locator('.pm-relation-funnel')).toBeVisible();
    await expect(page.locator('.pm-llm-layout')).toHaveCount(0);
    await expect(page.locator('.pm-cross-venue-funnel')).toHaveCount(0);
    await page.evaluate(() => (
      window as Window & { startPredictionSignalPolling: () => void }
    ).startPredictionSignalPolling());
    await expect(page.locator('.pm-candidate-table tbody tr')).toHaveCount(2);
  });

  test('same-venue actions never emit a Predict.fun mutation request', async ({ page }) => {
    await openPrediction(page);
    const predictMutations: string[] = [];
    page.on('request', (request) => {
      if (request.url().includes('predict.fun') && !['GET', 'HEAD'].includes(request.method())) {
        predictMutations.push(`${request.method()} ${request.url()}`);
      }
    });
    await page.locator('[data-action="participate"]').first().click();
    await expect(page.locator('.pm-modal')).toBeVisible();
    expect(predictMutations).toEqual([]);
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
    await expect(page.locator('[data-prediction-history-panel] tbody tr')).toHaveCount(5);
    await expect(page.locator('[data-prediction-history-panel] [data-action="participate"]')).toHaveCount(0);
    await expect(page.locator('.pm-venue-readiness')).toContainText('不可用');

    await openPrediction(page, 'unavailable');
    await expect(page.locator('.pm-status-line')).toContainText('Watcher 不可用');
    await expect(page.locator('.pm-metrics')).toHaveCount(0);
    await expect(page.locator('.pm-venue-readiness')).toContainText('不可用');
    await expect(page.locator('[data-prediction-history-panel] [data-action="participate"]')).toHaveCount(0);

    await openPrediction(page, 'unknown');
    await expect(page.locator('.pm-status-line')).toContainText('Watcher 不可用');
    await expect(page.locator('.pm-metrics')).toHaveCount(0);
    await expect(page.locator('.pm-venue-readiness')).toContainText('不可用');
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

  test('orders signals by recency and exposes 24h volume', async ({ page }) => {
    await openPrediction(page);
    const expectedVolumes = ['$9.7M', '$12.8M', '$15.4M', '$7.1M', '$6.8M'];
    for (const [index, volume] of expectedVolumes.entries()) {
      await expect(page.locator('[data-prediction-history-panel] tbody tr').nth(index).locator('[data-label="24h 成交量"]')).toHaveText(volume);
    }
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
    await page.route('**/api/prediction-arbitrage/history*', async (route) => {
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ items: [] }) });
    });
    await openPrediction(page);
    await expect(page.locator('.pm-event')).toHaveCount(0);
    await expect(page.locator('[data-prediction-history-panel]')).toContainText('还没有历史信号');
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
      await expect.poll(() => page.locator('.pm-venue-readiness').evaluate((node) => getComputedStyle(node).gridTemplateColumns.split(' ').length)).toBe(2);
      await expect(page.locator('.pm-metrics')).toHaveCount(0);
      await expect(page.locator('[data-prediction-history-panel]')).toBeVisible();
      await expect(page.locator('[data-prediction-history-panel]')).toContainText('24h 成交量');
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
