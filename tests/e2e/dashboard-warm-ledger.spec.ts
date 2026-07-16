import { expect, test, type Page } from '@playwright/test';

const warmLedger = {
  bg: '#F7F5F1',
  surface: '#FFFEFA',
  soft: '#F2EEE7',
  text: '#201D18',
  muted: '#746E64',
  accent: '#8B5E34',
  line: '#D8D2C8',
  primary: '#24211D',
  danger: '#B42318',
  success: '#2F855A',
} as const;

const rgb = {
  bg: 'rgb(247, 245, 241)',
  surface: 'rgb(255, 254, 250)',
  text: 'rgb(32, 29, 24)',
  muted: 'rgb(116, 110, 100)',
  accent: 'rgb(139, 94, 52)',
  line: 'rgb(216, 210, 200)',
  primary: 'rgb(36, 33, 29)',
  danger: 'rgb(180, 35, 24)',
  success: 'rgb(47, 133, 90)',
} as const;

async function installLedgerFixture(page: Page) {
  await page.route('**/api/dashboard', async (route) => {
    const response = await route.fetch();
    const fixture = await response.json();
    fixture.summary = { portfolio_value_hkd: '3064187.62', holding_value_hkd: '647547.98', cash_like_value_hkd: '2416639.64', holding_count: 4 };
    fixture.broker_summaries = [
      { broker: 'futu', display_name: '富途', portfolio_value_hkd: '971244.73', holding_value_hkd: '960926.44', cash_like_value_hkd: '10318.30', holding_count: 1 },
      { broker: 'tiger', display_name: '老虎', portfolio_value_hkd: '726091.55', holding_value_hkd: '700000.00', cash_like_value_hkd: '26091.55', holding_count: 1 },
      { broker: 'phillips', display_name: '辉立', portfolio_value_hkd: '628554.06', holding_value_hkd: '600000.00', cash_like_value_hkd: '28554.06', holding_count: 1 },
      { broker: 'eastmoney', display_name: '东方财富', portfolio_value_hkd: '730673.51', holding_value_hkd: '700000.00', cash_like_value_hkd: '30673.51', holding_count: 1 },
    ];
    fixture.trend_reports = {
      futu: {
        available: true,
        broker_label: '富途',
        market_label: '美股',
        report_date: '2026-07-16',
        data_date: '2026-07-15',
        generated_at: '2026-07-16 08:00',
        account_status: '已更新',
        buy_window: '美股常规交易时段',
        counts: { sell: 0, buy: 0, hold: 0, review: 0 },
        sell_actions: [], buy_actions: [], hold_actions: [], review_actions: [],
        audit: { candidates: [], excluded: {}, industry_concentration: [], data_sources: ['fixture'] },
      },
      eastmoney: {
        available: true,
        broker_label: '东方财富',
        market: 'CN',
        market_label: 'A股',
        report_date: '2026-07-16',
        data_date: '2026-07-15',
        generated_at: '2026-07-16T11:07:21+08:00',
        account_status: '账户数据非实时，执行前核对现金与持仓',
        buy_window: '09:30–10:00',
        counts: { sell: 0, buy: 1, hold: 0, review: 0 },
        sell_actions: [],
        review_actions: [],
        hold_actions: [],
        buy_actions: [{
          symbol: '600519', name: '贵州茅台', filter_price: '1501.00',
          close: '1500.00', temperature_prev: '温', temperature_curr: '热',
          phase: '小暑', strength: '97.7', industry: '白酒',
          industry_temperature: '热', market_cap: '19000', amount: '35',
          target_weight: '0.04', target_amount: '40000',
          estimated_shares: '26', estimated_initial_line: '1425.00',
        }],
        audit: {
          candidates: [], excluded: {}, industry_concentration: [],
          data_sources: ['fixture'],
        },
      },
    };
    fixture.holdings = [
      { market: 'US', symbol: 'AAPL', name: 'Apple', currency: 'USD', total_quantity: '10000', avg_cost_price: '180.00', market_value_hkd: '16380000.00', unrealized_pnl_pct: '16.67%', brokers: 'futu', broker_details: [{ broker: 'futu', market: 'US', symbol: 'AAPL', name: 'Apple', quantity: '10000', cost_value: '1800000.00', avg_cost_price: '180.00', market_value_hkd: '16380000.00', unrealized_pnl: '300000.00', unrealized_pnl_pct: '16.67%' }] },
      { market: 'US', symbol: 'QQQ', name: 'Nasdaq 100', currency: 'USD', total_quantity: '2', avg_cost_price: '500.00', market_value_hkd: '7800.00', unrealized_pnl_pct: '-2.00%', brokers: 'tiger', broker_details: [{ broker: 'tiger', market: 'US', symbol: 'QQQ', name: 'Nasdaq 100', quantity: '2', cost_value: '1000.00', avg_cost_price: '500.00', market_value_hkd: '7800.00', unrealized_pnl: '-20.00', unrealized_pnl_pct: '-2.00%' }] },
      { market: 'HK', symbol: '02840', name: 'SPDR 金', currency: 'HKD', total_quantity: '11', avg_cost_price: '2932.00', market_value_hkd: '31845.00', unrealized_pnl_pct: '-1.26%', brokers: 'phillips', broker_details: [{ broker: 'phillips', market: 'HK', symbol: '02840', name: 'SPDR 金', quantity: '11', avg_cost_price: '2932.00', market_value_hkd: '31845.00', unrealized_pnl: '-407.00', unrealized_pnl_pct: '-1.26%' }] },
      { market: 'CN', symbol: '600519', name: '贵州茅台', currency: 'CNY', total_quantity: '100', avg_cost_price: '1500.00', market_value_hkd: '165000.00', unrealized_pnl_pct: '10.00%', brokers: 'eastmoney', broker_details: [{ broker: 'eastmoney', market: 'CN', symbol: '600519', name: '贵州茅台', quantity: '100', avg_cost_price: '1500.00', market_value_hkd: '165000.00', unrealized_pnl: '15000.00', unrealized_pnl_pct: '10.00%' }] },
    ];
    await route.fulfill({ response, json: fixture });
  });
}

async function expectWarmSurface(page: Page, selector: string) {
  const surface = page.locator(selector);
  await expect(surface).toBeVisible();
  await expect(surface).toHaveCSS('background-color', rgb.surface);
  await expect(surface).toHaveCSS('border-top-color', rgb.line);
  await expect(surface).toHaveCSS('border-top-width', '1px');
}

async function expectContrastAtLeast(page: Page, selector: string, minimum: number) {
  const ratio = await page.locator(selector).evaluate((element) => {
    const parse = (color: string) => color.match(/[\d.]+/g)!.slice(0, 3).map(Number);
    const luminance = (color: string) => {
      const [red, green, blue] = parse(color).map((channel) => {
        const value = channel / 255;
        return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
      });
      return 0.2126 * red + 0.7152 * green + 0.0722 * blue;
    };
    const styles = getComputedStyle(element);
    const foreground = luminance(styles.color);
    const background = luminance(styles.backgroundColor);
    return (Math.max(foreground, background) + 0.05) / (Math.min(foreground, background) + 0.05);
  });
  expect(ratio, `${selector} contrast`).toBeGreaterThanOrEqual(minimum);
}

async function expectMobileTargetsAtLeast44(page: Page, surface: string, selector: string) {
  const targets = page.locator(surface).locator(selector);
  expect(await targets.count(), `${surface} should expose ${selector}`).toBeGreaterThan(0);
  for (const target of await targets.evaluateAll((elements) => elements.map((element) => ({
    height: element.getBoundingClientRect().height,
    label: element.getAttribute('aria-label') || (element.textContent || '').trim() || element.tagName,
  })))) {
    expect(target.height, `${surface}: ${target.label}`).toBeGreaterThanOrEqual(44);
  }
}

const brokers = [
  { key: 'futu', label: '富途', symbol: 'AAPL', portfolio: '971,244.73', holding: '960,926.44', cash: '10,318.30' },
  { key: 'tiger', label: '老虎', symbol: 'QQQ', portfolio: '726,091.55', holding: '700,000.00', cash: '26,091.55' },
  { key: 'phillips', label: '辉立', symbol: '02840', portfolio: '628,554.06', holding: '600,000.00', cash: '28,554.06' },
  { key: 'eastmoney', label: '东方财富', symbol: '600519', portfolio: '730,673.51', holding: '700,000.00', cash: '30,673.51' },
] as const;

test('renders the exact approved warm-ledger contract', async ({ page }) => {
  await installLedgerFixture(page);
  await page.goto('/');

  const tokens = await page.evaluate(() => {
    const styles = getComputedStyle(document.documentElement);
    return Object.fromEntries([
      '--bg', '--surface', '--surface-soft', '--text', '--muted',
      '--accent', '--line', '--primary', '--danger', '--success',
    ].map((name) => [name, styles.getPropertyValue(name).trim().toUpperCase()]));
  });
  expect(tokens).toEqual({
    '--bg': warmLedger.bg,
    '--surface': warmLedger.surface,
    '--surface-soft': warmLedger.soft,
    '--text': warmLedger.text,
    '--muted': warmLedger.muted,
    '--accent': warmLedger.accent,
    '--line': warmLedger.line,
    '--primary': warmLedger.primary,
    '--danger': warmLedger.danger,
    '--success': warmLedger.success,
  });
  await expect(page.locator('body')).toHaveCSS('background-color', rgb.bg);
  await expect(page.locator('body')).toHaveCSS('color', rgb.text);
  await expect(page.locator('#refresh-quotes')).toHaveCSS('background-color', rgb.accent);
  await expect(page.locator('.current-view-card')).toHaveCSS('background-color', rgb.primary);
  await expectWarmSurface(page, '.header-brand-panel');
  await expectWarmSurface(page, '.holdings-panel');
  await expect(page.locator('#last-refresh')).toHaveCSS('color', rgb.muted);
  await expect(page.locator('.research-chat-context .status-ok')).toHaveCSS('color', rgb.text);
});

test('switches every broker tab and card while preserving US-filtered ledgers', async ({ page }) => {
  await installLedgerFixture(page);
  await page.goto('/');

  await expect(page.getByRole('tab')).toHaveCount(4);
  await expect(page.getByRole('tabpanel')).toHaveCount(1);
  await expect(page.getByRole('tabpanel')).toHaveAttribute('id', 'account-holdings');
  for (const tab of await page.getByRole('tab').all()) {
    await expect(tab).toHaveAttribute('aria-controls', 'account-holdings');
  }
  await expect(page.locator('#current-view-value')).toHaveText('HKD 3,064,187.62');
  const desktopTargets = page.locator('button:visible, a:visible');
  for (const target of await desktopTargets.evaluateAll((elements) => elements.map((element) => ({
    height: element.getBoundingClientRect().height,
    label: (element.textContent || '').trim(),
    width: element.getBoundingClientRect().width,
  })))) {
    expect(Math.min(target.height, target.width), target.label).toBeGreaterThanOrEqual(24);
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  for (const broker of brokers) {
    const tab = page.getByRole('tab', { name: new RegExp(broker.label) });
    await tab.click();
    await expect(tab).toHaveAttribute('aria-selected', 'true');
    await expect(page.getByRole('tabpanel')).toHaveAttribute('aria-labelledby', `account-tab-${broker.key}`);
    await expect(page.locator(`#account-${broker.key}`)).toBeVisible();
    await expect(page.locator('.account-section')).toHaveCount(1);
    await expect(page.locator(`#account-${broker.key}`)).toContainText(broker.symbol);
  }

  await page.getByRole('button', { name: 'US', exact: true }).click();
  for (const broker of brokers) {
    await page.locator(`.broker-summary-card[data-broker="${broker.key}"]`).click();
    await expect(page.getByRole('tab', { name: new RegExp(broker.label) })).toHaveAttribute('aria-selected', 'true');
    const account = page.locator(`#account-${broker.key}`);
    await expect(account).toBeVisible();
    await expect(account).toContainText(`HKD ${broker.portfolio}`);
    await expect(account).toContainText(`持仓资产 HKD ${broker.holding}`);
    await expect(account).toContainText(`现金 HKD ${broker.cash}`);
    await expect(page.locator('#current-view-value')).toHaveText('HKD 3,064,187.62');
    await expect(page.locator('#current-view-holding-value')).toHaveText('持仓资产 HKD 647,547.98');
    await expect(page.locator('#current-view-cash-note')).toHaveText('现金类资产 HKD 2,416,639.64 · 持仓 4');
    await expect(page.getByRole('button', { name: 'US', exact: true })).toHaveClass(/active/);
    await expect(page.locator('.account-section')).toHaveCount(1);
    await expect(page.getByText('02840', { exact: true })).toHaveCount(0);
    await expect(page.getByText('600519', { exact: true })).toHaveCount(0);
    if (broker.key === 'futu' || broker.key === 'tiger') {
      await expect(page.locator('.account-holding-row')).toHaveCount(1);
      await expect(account).toContainText(broker.symbol);
    } else {
      await expect(page.locator('.account-holding-row')).toHaveCount(0);
      await expect(account).toContainText('当前筛选下没有持仓');
    }
  }
  await page.locator('.broker-summary-card[data-broker="futu"]').click();
  await expect(page.locator('.account-holding-quantity')).toContainText('10,000');
  await expect(page.locator('.account-holding-market-value')).toContainText('HKD 16,380,000.00');
  await expect(page.locator('.account-holding-pnl.pnl-profit')).toHaveCSS('color', rgb.danger);
  await page.locator('.broker-summary-card[data-broker="tiger"]').click();
  await expect(page.locator('.account-holding-pnl.pnl-loss')).toHaveCSS('color', rgb.success);
  await page.locator('.account-holding-row').hover();
  await expect(page.locator('.account-holding-pnl.pnl-loss')).toHaveCSS('background-color', rgb.surface);
  await page.locator('.account-holding-actions [data-detail-mode="decision"]').click();
  await page.locator('.header-brand-panel').hover();
  await expect(page.locator('.account-holding-row')).toHaveClass(/active-row/);
  await expect(page.locator('.account-holding-pnl.pnl-loss')).toHaveCSS('background-color', rgb.surface);
  await expectContrastAtLeast(page, '.account-holding-pnl.pnl-loss', 4.5);
  await expect(page.getByRole('button', { name: '现金' })).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test('opens every warm-ledger destination, using real UI paths where available', async ({ page }) => {
  await installLedgerFixture(page);
  await page.goto('/');

  await page.getByRole('button', { name: '凯利实验室' }).click();
  await expectWarmSurface(page, '.kelly-lab-panel');
  await page.getByRole('button', { name: '返回持仓' }).click();

  await page.getByRole('button', { name: '策略回测' }).click();
  await expectWarmSurface(page, '#standard-backtest-workspace');
  await page.getByRole('button', { name: '返回持仓' }).click();

  await page.getByRole('button', { name: '当天趋势报告' }).click();
  await expectWarmSurface(page, '.trend-report-workspace');
  await page.getByRole('button', { name: '返回持仓' }).click();

  await page.locator('.account-holding-actions [data-detail-mode="decision"]').click();
  await expectWarmSurface(page, '.symbol-detail-panel.inline-symbol-detail');
  await expect(page.locator('[data-research-chat]')).toHaveCount(0);
  // The display-only dashboard has no reachable research-chat trigger; activate its existing surface directly.
  await page.evaluate(() => (window as any).openResearchChat('US:AAPL:Apple:0'));
  await expectWarmSurface(page, '.research-chat-modal');
  await expect(page.locator('.research-chat-context .status-ok')).toHaveCSS('color', rgb.text);
  await page.getByRole('button', { name: '关闭' }).click();
  await expect(page.locator('.research-chat-modal')).toBeHidden();
  await page.getByRole('button', { name: '收起' }).click();
  await expect(page.locator('.symbol-detail-panel.inline-symbol-detail')).toHaveCount(0);
});

test('keeps four equal tabs and workspaces usable on mobile', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await installLedgerFixture(page);
  await page.goto('/');

  const tabs = page.locator('#account-tabs [role="tab"]');
  await expect(tabs).toHaveCount(4);
  const tabLayout = await tabs.evaluateAll((elements) => elements.map((element) => element.getBoundingClientRect().width));
  expect(Math.max(...tabLayout) - Math.min(...tabLayout)).toBeLessThanOrEqual(1);
  await expect(page.locator('#account-tabs')).toHaveCSS('overflow-x', 'hidden');
  await expect(page.locator('#account-tabs')).not.toHaveCSS('position', 'sticky');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);

  for (const index of [0, 3]) {
    const tab = tabs.nth(index);
    await tab.focus();
    await expect(tab).toBeFocused();
    const focus = await tab.evaluate((element) => {
      const tabRect = element.getBoundingClientRect();
      const listRect = element.parentElement!.getBoundingClientRect();
      return {
        boxShadow: getComputedStyle(element).boxShadow,
        inside: tabRect.left >= listRect.left && tabRect.right <= listRect.right,
      };
    });
    expect(focus.boxShadow).toContain(rgb.accent);
    expect(focus.boxShadow).toContain('inset');
    expect(focus.inside).toBe(true);
  }

  await page.emulateMedia({ forcedColors: 'active' });
  for (const index of [0, 3]) {
    const tab = tabs.nth(index);
    await tab.focus();
    await expect(tab).toHaveCSS('outline-style', 'solid');
    await expect(tab).toHaveCSS('outline-width', '3px');
    await expect(tab).toHaveCSS('outline-offset', '-3px');
  }
  await page.emulateMedia({ forcedColors: 'none' });

  await tabs.first().focus();
  await page.keyboard.press('ArrowLeft');
  await expect(page.getByRole('tab', { name: /东方财富/ })).toBeFocused();
  await expect(page.getByRole('tab', { name: /东方财富/ })).toHaveAttribute('aria-selected', 'true');
  await page.keyboard.press('Home');
  await expect(page.getByRole('tab', { name: /富途/ })).toBeFocused();
  await page.keyboard.press('End');
  await expect(page.getByRole('tab', { name: /东方财富/ })).toBeFocused();
  await page.keyboard.press('ArrowRight');
  await expect(page.getByRole('tab', { name: /富途/ })).toBeFocused();

  await expectMobileTargetsAtLeast44(page, 'body', [
    '#account-tabs [role="tab"]:visible',
    '#header-market-filters button:visible',
    '.strategy-tools button:visible',
    '#refresh-quotes:visible',
    '.account-holding-actions button:visible',
  ].join(','));

  await page.getByRole('tab', { name: /老虎/ }).click();
  await page.getByRole('button', { name: '凯利实验室' }).click();
  await expect(page.locator('.dashboard-shell')).toHaveClass(/tool-workspace-view/);
  await expect(page.locator('.header-assets-panel')).toBeHidden();
  await expect(page.locator('.kelly-lab-panel')).toHaveCSS('background-color', rgb.surface);
  await expect(page.locator('.kelly-lab-panel')).toHaveCSS('border-top-color', rgb.line);
  await expectMobileTargetsAtLeast44(page, 'body', '#return-to-portfolio:visible, .kelly-lab-panel button:visible');
  await page.getByRole('button', { name: '返回持仓' }).click();
  await expect(page.getByRole('tab', { name: /老虎/ })).toHaveAttribute('aria-selected', 'true');

  await page.getByRole('button', { name: '策略回测' }).click();
  await expect(page.locator('#standard-backtest-workspace')).toBeVisible();
  await expectMobileTargetsAtLeast44(page, '#standard-backtest-workspace', 'button:visible, input:visible, select:visible');
  await page.getByRole('button', { name: '返回持仓' }).click();

  await page.getByRole('tab', { name: /富途/ }).click();
  await page.getByRole('button', { name: '当天趋势报告' }).click();
  await expect(page.locator('#trend-report-workspace')).toBeVisible();
  await expectMobileTargetsAtLeast44(page, 'body', '#return-to-portfolio:visible, #trend-report-workspace button:visible');
  await page.getByRole('button', { name: '返回持仓' }).click();

  await page.locator('.account-holding-actions [data-detail-mode="decision"]').click();
  await expect(page.locator('.symbol-detail-panel.inline-symbol-detail')).toBeVisible();
  // The language toggle renderer is dormant in the current decision flow; mount its production markup to verify its mobile CSS contract.
  await page.evaluate(() => {
    const panel = document.querySelector('.symbol-detail-panel.inline-symbol-detail');
    panel?.insertAdjacentHTML('beforeend', (window as any).renderLanguageToggle());
  });
  await expectMobileTargetsAtLeast44(page, '.symbol-detail-panel.inline-symbol-detail', '.decision-tab:visible, [data-back-to-holdings]:visible, .language-toggle button:visible');
  await page.getByRole('button', { name: '收起' }).click();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test('aligns the A-share report with the 1600px shell and scrolls only the buy table', async ({ page }) => {
  await page.setViewportSize({ width: 1920, height: 1080 });
  await installLedgerFixture(page);
  await page.goto('/');
  await page.getByRole('tab', { name: /东方财富/ }).click();
  const holdings = await page.locator('.holdings-panel').boundingBox();
  await page.getByRole('button', { name: '当天趋势报告' }).click();

  const geometry = await page.evaluate(() => {
    const shell = document.querySelector('.dashboard-shell')!.getBoundingClientRect();
    const header = document.querySelector('.dashboard-header')!.getBoundingClientRect();
    const report = document.querySelector('#trend-report-workspace')!.getBoundingClientRect();
    const stage = document.querySelector('.cn-trend-buy')!;
    const stageStyle = getComputedStyle(stage);
    return {
      shellWidth: shell.width,
      headerLeft: header.left,
      headerRight: header.right,
      reportLeft: report.left,
      reportRight: report.right,
      pageFits: document.documentElement.scrollWidth <= window.innerWidth,
      stageClientWidth: stage.clientWidth,
      stageScrollWidth: stage.scrollWidth,
      overflowX: stageStyle.overflowX,
    };
  });
  expect(geometry.shellWidth).toBeCloseTo(1600, 0);
  expect(geometry.reportLeft).toBeCloseTo(geometry.headerLeft, 0);
  expect(geometry.reportRight).toBeCloseTo(geometry.headerRight, 0);
  expect(geometry.reportLeft).toBeCloseTo(holdings!.x, 0);
  expect(geometry.reportRight).toBeCloseTo(holdings!.x + holdings!.width, 0);
  expect(geometry.pageFits).toBe(true);
  expect(geometry.overflowX).toBe('auto');
  expect(geometry.stageScrollWidth).toBeGreaterThan(geometry.stageClientWidth);
  const buyStage = page.locator('.cn-trend-buy');
  await expect(buyStage).toHaveAttribute('tabindex', '0');
  await expect(buyStage).toHaveAttribute('aria-label', '正式买入计划，可横向滚动');
  await page.keyboard.press('Tab');
  await page.keyboard.press('Tab');
  await expect(buyStage).toBeFocused();
  await expect(buyStage).toHaveCSS('outline-style', 'solid');
  await expect(buyStage).toHaveCSS('outline-width', '3px');
  await expect(page.locator('.cn-trend-price-sources')).toHaveCSS('color', rgb.muted);
  await page.setViewportSize({ width: 375, height: 844 });
  await expect(buyStage).toHaveAttribute('tabindex', '-1');
  await expect(buyStage).toHaveAttribute('aria-label', '正式买入计划');
  await page.setViewportSize({ width: 1920, height: 1080 });
  await expect(buyStage).toHaveAttribute('tabindex', '0');
  await expect(buyStage).toHaveAttribute('aria-label', '正式买入计划，可横向滚动');
});

test('keeps the A-share report card-based with no page overflow on mobile', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 844 });
  await installLedgerFixture(page);
  await page.goto('/');
  await page.getByRole('tab', { name: /东方财富/ }).click();
  await page.getByRole('button', { name: '当天趋势报告' }).click();

  await expect(page.locator('.cn-trend-buy')).toHaveCSS('overflow-x', 'hidden');
  await expect(page.locator('.cn-trend-buy')).toHaveAttribute('tabindex', '-1');
  await expect(page.locator('.cn-trend-buy')).toHaveAttribute('aria-label', '正式买入计划');
  await page.keyboard.press('Tab');
  await expect(page.locator('.cn-trend-buy')).not.toBeFocused();
  for (const head of await page.locator('.cn-trend-table thead').all()) {
    await expect(head).toBeHidden();
  }
  await expect(page.locator('.cn-trend-buy .cn-trend-card')).toHaveCSS('display', 'grid');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});
