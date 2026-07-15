import { expect, test, type Page } from '@playwright/test';

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
    fixture.holdings = [
      { market: 'US', symbol: 'AAPL', name: 'Apple', currency: 'USD', total_quantity: '10000', avg_cost_price: '180.00', market_value_hkd: '16380000.00', unrealized_pnl_pct: '16.67%', brokers: 'futu', broker_details: [{ broker: 'futu', market: 'US', symbol: 'AAPL', name: 'Apple', quantity: '10000', cost_value: '1800000.00', avg_cost_price: '180.00', market_value_hkd: '16380000.00', unrealized_pnl: '300000.00', unrealized_pnl_pct: '16.67%' }] },
      { market: 'US', symbol: 'QQQ', name: 'Nasdaq 100', currency: 'USD', total_quantity: '2', avg_cost_price: '500.00', market_value_hkd: '7800.00', unrealized_pnl_pct: '-2.00%', brokers: 'tiger', broker_details: [{ broker: 'tiger', market: 'US', symbol: 'QQQ', name: 'Nasdaq 100', quantity: '2', cost_value: '1000.00', avg_cost_price: '500.00', market_value_hkd: '7800.00', unrealized_pnl: '-20.00', unrealized_pnl_pct: '-2.00%' }] },
      { market: 'HK', symbol: '02840', name: 'SPDR 金', currency: 'HKD', total_quantity: '11', avg_cost_price: '2932.00', market_value_hkd: '31845.00', unrealized_pnl_pct: '-1.26%', brokers: 'phillips', broker_details: [{ broker: 'phillips', market: 'HK', symbol: '02840', name: 'SPDR 金', quantity: '11', avg_cost_price: '2932.00', market_value_hkd: '31845.00', unrealized_pnl: '-407.00', unrealized_pnl_pct: '-1.26%' }] },
      { market: 'CN', symbol: '600519', name: '贵州茅台', currency: 'CNY', total_quantity: '100', avg_cost_price: '1500.00', market_value_hkd: '165000.00', unrealized_pnl_pct: '10.00%', brokers: 'eastmoney', broker_details: [{ broker: 'eastmoney', market: 'CN', symbol: '600519', name: '贵州茅台', quantity: '100', avg_cost_price: '1500.00', market_value_hkd: '165000.00', unrealized_pnl: '15000.00', unrealized_pnl_pct: '10.00%' }] },
    ];
    await route.fulfill({ response, json: fixture });
  });
}

test('switches every broker card while keeping global assets and market filter', async ({ page }) => {
  await installLedgerFixture(page);
  await page.goto('/');

  await expect(page.getByRole('tab')).toHaveCount(4);
  await expect(page.getByRole('tab', { name: /富途/ })).toHaveAttribute('aria-selected', 'true');
  await expect(page.locator('#current-view-value')).toHaveText('HKD 3,064,187.62');
  await expect(page.locator('#account-futu')).toBeVisible();
  await expect(page.locator('.account-section')).toHaveCount(1);
  await expect(page.locator('.account-holding-quantity')).toContainText('10,000');
  await expect(page.locator('.account-holding-market-value')).toContainText('HKD 16,380,000.00');
  await expect(page.getByText('02840', { exact: true })).toHaveCount(0);
  await expect(page.locator('.account-holding-pnl.pnl-profit')).toHaveCSS('color', 'rgb(185, 28, 28)');

  await page.getByRole('button', { name: 'US', exact: true }).click();
  await page.getByRole('tab', { name: /老虎/ }).click();
  await expect(page.locator('#account-tiger')).toBeVisible();
  await expect(page.locator('.account-holding-pnl.pnl-loss')).toHaveCSS('color', 'rgb(21, 128, 61)');

  for (const broker of ['phillips', 'eastmoney', 'futu', 'tiger']) {
    await page.locator(`.broker-summary-card[data-broker="${broker}"]`).click();
    await expect(page.locator(`#account-${broker}`)).toBeVisible();
    await expect(page.locator('#current-view-value')).toHaveText('HKD 3,064,187.62');
    await expect(page.getByRole('button', { name: 'US', exact: true })).toHaveClass(/active/);
    await expect(page.locator('.account-section')).toHaveCount(1);
  }
  await expect(page.getByRole('button', { name: '现金' })).toHaveCount(0);
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

  await page.getByRole('tab', { name: /老虎/ }).click();
  await page.getByRole('button', { name: '凯利实验室' }).click();
  await expect(page.locator('.dashboard-shell')).toHaveClass(/tool-workspace-view/);
  await expect(page.locator('.header-assets-panel')).toBeHidden();
  await expect(page.locator('.kelly-lab-panel')).toHaveCSS('background-color', 'rgb(255, 255, 255)');
  await expect(page.locator('.kelly-lab-panel')).toHaveCSS('border-top-color', 'rgb(214, 211, 209)');
  await page.getByRole('button', { name: '返回持仓' }).click();
  await expect(page.getByRole('tab', { name: /老虎/ })).toHaveAttribute('aria-selected', 'true');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});
