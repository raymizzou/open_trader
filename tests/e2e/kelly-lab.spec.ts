import { expect, test, type Locator } from '@playwright/test';

async function expectNoEditableControls(scope: Locator) {
  await expect(scope.locator('input, textarea, select, [contenteditable]:not([contenteditable="false"])')).toHaveCount(0);
  for (const role of ['textbox', 'combobox', 'spinbutton', 'switch'] as const) {
    await expect(scope.getByRole(role)).toHaveCount(0);
  }
}

async function expectKellyDerivationFits(scope: Locator, expectedRowCount: number) {
  await expect(scope).toBeVisible();
  const rows = scope.locator('.kelly-derivation-grid > div');
  await expect(rows).toHaveCount(expectedRowCount);
  const layout = await rows.evaluateAll((rows) => rows.map((row) => {
    const rect = row.getBoundingClientRect();
    return {
      bottom: rect.bottom,
      content: Array.from(row.querySelectorAll('span, strong')).map((element) => {
        const contentRect = element.getBoundingClientRect();
        return {
          bottom: contentRect.bottom,
          fits: element.scrollWidth <= element.clientWidth,
          left: contentRect.left,
          right: contentRect.right,
          top: contentRect.top,
        };
      }),
      left: rect.left,
      right: rect.right,
      top: rect.top,
    };
  }));
  for (const row of layout) {
    for (const content of row.content) {
      expect(content.fits).toBe(true);
    }
  }
  for (let index = 0; index < layout.length; index += 1) {
    for (let otherIndex = index + 1; otherIndex < layout.length; otherIndex += 1) {
      const first = layout[index];
      const second = layout[otherIndex];
      const overlaps = first.left < second.right
        && first.right > second.left
        && first.top < second.bottom
        && first.bottom > second.top;
      expect(overlaps).toBe(false);
    }
  }
  const content = layout.flatMap((row) => row.content);
  for (let index = 0; index < content.length; index += 1) {
    for (let otherIndex = index + 1; otherIndex < content.length; otherIndex += 1) {
      const first = content[index];
      const second = content[otherIndex];
      const overlaps = first.left < second.right
        && first.right > second.left
        && first.top < second.bottom
        && first.bottom > second.top;
      expect(overlaps).toBe(false);
    }
  }
}

async function expectKellyDerivationRow(scope: Locator, label: string, value: string) {
  const row = scope.locator('.kelly-derivation-grid > div').filter({ hasText: label });
  await expect(row).toHaveCount(1);
  await expect(row.locator('span')).toHaveText(label);
  await expect(row.locator('strong')).toHaveText(value);
  await expect(row.locator('span')).toBeVisible();
  await expect(row.locator('strong')).toBeVisible();
}

test('renders Kelly lab without a holding-level Kelly entry', async ({ page }) => {
  await page.goto('/');

  await expect(page.getByRole('heading', { name: '持仓列表' })).toBeVisible();
  await expect(page.getByRole('heading', { name: '模拟盘策略实验室' })).toHaveCount(0);
  await expect(page.getByText('趋势回调 20D Mock US 第一批')).toHaveCount(0);
  await page.getByRole('button', { name: '凯利实验室' }).click();

  await expect(page.getByRole('heading', { name: '模拟盘策略实验室' })).toBeVisible();
  const kellyLabPanel = page.getByLabel('Kelly 模拟盘策略实验室');
  await expect(page.getByRole('tab', { name: /趋势回调 20D Mock US 第一批/ })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByRole('tab', { name: /趋势回调 20D Mock HK 第一批/ })).toHaveAttribute('aria-selected', 'false');
  await expect(page.getByRole('tab', { name: /突破 10D Mock 第一批/ })).toHaveAttribute('aria-selected', 'false');
  await expectNoEditableControls(kellyLabPanel);
  await expect(page.getByText('Mock 状态样本')).toHaveCount(0);
  await expect(page.getByText('状态说明')).toHaveCount(0);
  const usTrendCard = page.locator('.kelly-experiment-card').filter({ has: page.getByRole('heading', { name: '趋势回调 20D Mock US 第一批' }) });
  await expect(usTrendCard.getByText('市场')).toBeVisible();
  await expect(usTrendCard.getByText('US', { exact: true })).toBeVisible();
  await expect(usTrendCard.getByText('模拟资金池')).toBeVisible();
  await expect(usTrendCard.getByText('USD 30000').first()).toBeVisible();
  const usCapital = usTrendCard.getByLabel('Kelly 策略资金');
  await expect(usCapital).toBeVisible();
  await expect(usCapital.getByText('可用资金', { exact: true })).toBeVisible();
  await expect(usCapital.getByText('USD 21,540')).toBeVisible();
  await expect(usCapital.getByText('下一笔下单影响')).toBeVisible();
  await expect(usCapital.locator('.kelly-capital-pane', { hasText: '标的占用' }).getByText('US.RAM', { exact: true })).toBeVisible();
  for (const symbol of ['US.DRAM', 'US.RAM', 'US.SOXX']) {
    await expect(page.getByLabel('Kelly 标的状态').getByText(symbol)).toBeVisible();
  }
  for (const symbol of ['HK.02840', 'US.MSFT', 'US.TSM']) {
    await expect(page.getByLabel('Kelly 标的状态').getByText(symbol)).toHaveCount(0);
  }
  const symbolStates = page.getByLabel('Kelly 标的状态');
  await expect(symbolStates.getByRole('checkbox')).toHaveCount(0);
  for (const buttonName of ['新策略', '保存配置', '添加标的']) {
    await expect(page.getByRole('button', { name: buttonName })).toHaveCount(0);
  }
  await expect(page.getByRole('heading', { name: '趋势回调 20D Mock US 第一批' })).toBeVisible();
  const orderSync = page.getByLabel('Kelly 订单同步');
  await expect(orderSync.getByText('同步成功')).toBeVisible();
  await expect(orderSync.getByText('富途模拟盘订单已同步。')).toBeVisible();
  await expect(orderSync.getByText('SIMULATE')).toBeVisible();
  await expect(orderSync.getByText('2026-07-08 10:08')).toBeVisible();
  await expect(orderSync.getByText('可以继续扫描入场与退出信号。')).toBeVisible();
  await expect(orderSync.getByText('US.RAM')).toBeVisible();
  await expect(orderSync.getByText('SIM-10001')).toBeVisible();
  await expect(orderSync.getByText('买入')).toBeVisible();
  await expect(orderSync.getByText('12.34')).toHaveCount(2);
  await expect(orderSync.getByText('800')).toHaveCount(2);
  await expect(orderSync.getByText('已成交')).toBeVisible();
  await expect(orderSync.getByText('US.MSFT')).toHaveCount(0);
  const orderExecution = page.getByLabel('Kelly 订单执行');
  await expect(orderExecution.getByText('部分执行')).toBeVisible();
  await expect(orderExecution.getByText('Kelly 订单执行存在失败或跳过项。')).toBeVisible();
  await expect(orderExecution.getByText('DRY_RUN')).toBeVisible();
  await expect(orderExecution.getByText('2026-07-10 13:32').first()).toBeVisible();
  await expect(orderExecution.getByText('US.RAM')).toBeVisible();
  await expect(orderExecution.getByText('预演').first()).toBeVisible();
  await expect(orderExecution.getByText('已跳过')).toBeVisible();
  await expect(orderExecution.getByText('missing order quantity')).toBeVisible();
  await expect(orderExecution.getByText('US.MSFT')).toHaveCount(0);
  await expect(symbolStates.getByText('观察中 → 待下单 → 持仓中 → 待退出 → 已完成')).toBeVisible();
  await expect(symbolStates.getByText('该标的在策略监控范围内，但当前没有入场信号，也没有持仓。')).toBeVisible();
  const pendingEntryNarrative = symbolStates.getByText('入场规则触发，仓位计算与风控检查待执行。');
  await expect(pendingEntryNarrative).toHaveCount(2);
  await expect(pendingEntryNarrative.first()).toBeVisible();
  await expect(symbolStates.getByText(/风控通过/)).toHaveCount(0);
  await expect(symbolStates.getByText(/Kelly 建议单标的仓位 4%/)).toHaveCount(0);
  await expect(symbolStates.getByText('模拟盘买入已成交，这笔策略样本正在进行中。')).toBeVisible();
  await expect(symbolStates.getByText('这笔持仓已经触发退出规则，但卖出还没有完成。')).toHaveCount(0);
  await expect(page.getByText('样本充足')).toBeVisible();
  await expect(page.getByLabel('实验参与标的')).toHaveCount(0);
  await expect(page.getByText('策略详情')).toBeVisible();
  const strategyRules = page.getByLabel('Kelly 策略详情');
  await expect(strategyRules.getByText('价格回调到 20 日均线 ±1% 内，且 50 日均线斜率向上。')).toBeVisible();
  await expect(strategyRules.getByText('跌破 20 日均线 3% 或跌破最近波段低点。')).toBeVisible();
  await expect(strategyRules.getByText('价格达到入场价 + 2R 时卖出 50%。')).toBeVisible();
  await expect(strategyRules.getByText('剩余仓位收盘跌破 10 日均线时退出。')).toBeVisible();
  await expect(strategyRules.getByText('持有满 20 个交易日仍未触发止盈或止损则退出。')).toBeVisible();
  await expect(page.getByText('第一目标')).toHaveCount(0);
  await expect(page.getByText('延续')).toHaveCount(0);
  const parameterDerivation = page.getByLabel('Kelly 参数推导');
  await expect(parameterDerivation.getByText('参数推导')).toBeVisible();
  await expect(parameterDerivation.getByText('样本状态')).toBeVisible();
  await expect(parameterDerivation.getByText('样本充足')).toBeVisible();
  await expect(parameterDerivation.getByText('已完成样本')).toBeVisible();
  await expect(parameterDerivation.getByText('208', { exact: true })).toBeVisible();
  await expect(parameterDerivation.getByText('进行中样本')).toBeVisible();
  await expect(parameterDerivation.getByText('3', { exact: true })).toBeVisible();
  await expect(parameterDerivation.getByText('参数来源')).toBeVisible();
  await expect(parameterDerivation.getByText('富途模拟盘订单样本')).toBeVisible();
  await expect(parameterDerivation.getByText('跳过订单')).toBeVisible();
  await expect(parameterDerivation.getByText('116 赢 / 92 亏')).toBeVisible();
  await expect(parameterDerivation.getByText('Full Kelly')).toBeVisible();
  await expect(parameterDerivation.getByText('建议仓位')).toBeVisible();
  await expect(parameterDerivation.getByText('4%', { exact: true })).toBeVisible();
  await expect(parameterDerivation.getByText('来源样本时间')).toBeVisible();
  await expect(parameterDerivation.getByText('2026-07-11 11:59')).toBeVisible();
  await expect(parameterDerivation.getByText('最近完成样本')).toBeVisible();
  await expect(parameterDerivation.getByText('2026-07-11 11:58')).toBeVisible();
  await expect(parameterDerivation.getByText('最近计算')).toBeVisible();
  await expect(parameterDerivation.getByText('2026-07-11 12:00')).toBeVisible();
  await expectKellyDerivationFits(parameterDerivation, 14);
  await expect(page.getByRole('heading', { name: '趋势回调 20D Mock HK 第一批' })).toHaveCount(0);
  await page.getByRole('tab', { name: /趋势回调 20D Mock HK 第一批/ }).click();
  await expect(page.getByRole('tab', { name: /趋势回调 20D Mock US 第一批/ })).toHaveAttribute('aria-selected', 'false');
  await expect(page.getByRole('tab', { name: /趋势回调 20D Mock HK 第一批/ })).toHaveAttribute('aria-selected', 'true');
  await expectNoEditableControls(kellyLabPanel);
  await expect(page.getByRole('heading', { name: '趋势回调 20D Mock US 第一批' })).toHaveCount(0);
  await expect(page.getByRole('heading', { name: '趋势回调 20D Mock HK 第一批' })).toBeVisible();
  const hkTrendCard = page.locator('.kelly-experiment-card').filter({ has: page.getByRole('heading', { name: '趋势回调 20D Mock HK 第一批' }) });
  await expect(hkTrendCard.getByText('HK', { exact: true })).toBeVisible();
  await expect(hkTrendCard.getByText('HKD 200000').first()).toBeVisible();
  const hkCapital = hkTrendCard.getByLabel('Kelly 策略资金');
  await expect(hkCapital.locator('dd', { hasText: 'HKD 155,000' })).toBeVisible();
  await expect(page.getByLabel('Kelly 标的状态').getByText('HK.02840')).toBeVisible();
  await expect(page.getByLabel('Kelly 标的状态').getByText('US.DRAM')).toHaveCount(0);
  await expect(page.getByLabel('Kelly 标的状态').getByText('这笔持仓已经触发退出规则，但卖出还没有完成。')).toBeVisible();
  const insufficientDerivation = page.getByLabel('Kelly 参数推导');
  await expect(insufficientDerivation.getByText('样本状态')).toBeVisible();
  await expect(insufficientDerivation.getByText('样本不足')).toBeVisible();
  await expect(insufficientDerivation.getByText('0%', { exact: true })).toHaveCount(3);
  await page.getByRole('tab', { name: /突破 10D Mock 第一批/ }).click();
  await expect(page.getByRole('tab', { name: /趋势回调 20D Mock US 第一批/ })).toHaveAttribute('aria-selected', 'false');
  await expect(page.getByRole('tab', { name: /趋势回调 20D Mock HK 第一批/ })).toHaveAttribute('aria-selected', 'false');
  await expect(page.getByRole('tab', { name: /突破 10D Mock 第一批/ })).toHaveAttribute('aria-selected', 'true');
  await expectNoEditableControls(kellyLabPanel);
  await expect(page.getByRole('heading', { name: '趋势回调 20D Mock US 第一批' })).toHaveCount(0);
  await expect(page.getByRole('heading', { name: '突破 10D Mock 第一批' })).toBeVisible();
  const breakoutCard = page.locator('.kelly-experiment-card').filter({ has: page.getByRole('heading', { name: '突破 10D Mock 第一批' }) });
  await expect(breakoutCard.getByText('US', { exact: true })).toBeVisible();
  await expect(breakoutCard.getByText('USD 30000').first()).toBeVisible();
  for (const symbol of ['US.MSFT', 'US.TSM']) {
    await expect(page.getByLabel('Kelly 标的状态').getByText(symbol)).toBeVisible();
  }
  for (const symbol of ['US.DRAM', 'US.RAM', 'US.SOXX', 'HK.02840']) {
    await expect(page.getByLabel('Kelly 标的状态').getByText(symbol)).toHaveCount(0);
  }
  const failedOrderSync = page.getByLabel('Kelly 订单同步');
  await expect(failedOrderSync.getByText('同步失败', { exact: true })).toBeVisible();
  await expect(failedOrderSync.getByText('模拟盘订单同步失败：OpenD 不可用。')).toBeVisible();
  await expect(failedOrderSync.getByText('本轮不下单，保留现有订单状态。')).toBeVisible();
  await expect(failedOrderSync.getByText('US.MSFT')).toBeVisible();
  await expect(failedOrderSync.getByText('SIM-20001')).toBeVisible();
  await expect(failedOrderSync.getByText('拒单')).toBeVisible();
  await expect(failedOrderSync.getByText('505.10')).toBeVisible();
  await expect(failedOrderSync.getByText('US.RAM')).toHaveCount(0);
  const failedOrderExecution = page.getByLabel('Kelly 订单执行');
  await expect(failedOrderExecution.getByText('执行失败', { exact: true }).first()).toBeVisible();
  await expect(failedOrderExecution.getByText('Kelly 订单执行存在失败或跳过项。')).toBeVisible();
  await expect(failedOrderExecution.getByText('OpenD disconnected')).toBeVisible();
  await expect(failedOrderExecution.getByText('US.RAM')).toHaveCount(0);
  await expect(page.getByLabel('Kelly 策略详情').getByText('价格放量突破近 10 个交易日高点，成交量不低于 1.5 倍均量。')).toBeVisible();
  await page.getByRole('button', { name: '返回主页' }).click();
  await expect(page.getByRole('heading', { name: '模拟盘策略实验室' })).toHaveCount(0);

  const aaplRow = page.getByRole('row').filter({ hasText: 'AAPL' }).first();
  await expect(aaplRow.getByRole('button', { name: '凯利' })).toHaveCount(0);
  await expect(page.getByRole('heading', { name: /凯利仓位 · US\.AAPL/ })).toHaveCount(0);
});

test('renders sufficient and insufficient Kelly derivations without mobile overflow', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  await page.getByRole('button', { name: '凯利实验室' }).click();

  const sufficientDerivation = page.getByLabel('Kelly 参数推导');
  for (const [label, value] of [
    ['样本状态', '样本充足'],
    ['已完成样本', '208'],
    ['进行中样本', '3'],
    ['来源样本时间', '2026-07-11 11:59'],
    ['最近完成样本', '2026-07-11 11:58'],
    ['最近计算', '2026-07-11 12:00'],
  ]) {
    await expectKellyDerivationRow(sufficientDerivation, label, value);
  }
  await expectKellyDerivationFits(sufficientDerivation, 14);

  await page.getByRole('tab', { name: /趋势回调 20D Mock HK 第一批/ }).click();
  const insufficientDerivation = page.getByLabel('Kelly 参数推导');
  for (const [label, value] of [
    ['样本状态', '样本不足'],
    ['已完成样本', '0'],
    ['进行中样本', '0'],
    ['来源样本时间', '2026-07-11 11:59'],
    ['最近计算', '2026-07-11 12:00'],
  ]) {
    await expectKellyDerivationRow(insufficientDerivation, label, value);
  }
  await expectKellyDerivationFits(insufficientDerivation, 13);
});

test('renders stale Kelly strategy stats as unavailable without controls at mobile viewport', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.route('**/api/dashboard', async (route) => {
    const response = await route.fetch();
    const fixture = await response.json();
    fixture.kelly_lab = fixture.kelly_lab_unavailable;
    await route.fulfill({ response, json: fixture });
  });

  await page.goto('/');
  await page.getByRole('button', { name: '凯利实验室' }).click();

  const kellyLabPanel = page.getByLabel('Kelly 模拟盘策略实验室');
  await expect(kellyLabPanel.getByText('不可用', { exact: true })).toBeVisible();
  await expect(kellyLabPanel.locator('.kelly-lab-empty')).toHaveText('kelly_strategy_stats.json stale: source trade sample timestamp does not match');
  await expectNoEditableControls(kellyLabPanel);
});
