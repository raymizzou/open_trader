import { expect, test } from '@playwright/test';

test('renders Kelly lab and opens holding Kelly detail', async ({ page }) => {
  await page.goto('/');

  await expect(page.getByRole('heading', { name: '持仓列表' })).toBeVisible();
  await expect(page.getByRole('heading', { name: '模拟盘策略实验室' })).toHaveCount(0);
  await expect(page.getByText('趋势回调 20D 第一批')).toHaveCount(0);
  await page.getByRole('button', { name: '凯利实验室' }).click();

  await expect(page.getByRole('heading', { name: '模拟盘策略实验室' })).toBeVisible();
  await expect(page.getByRole('tab', { name: /趋势回调 20D 第一批/ })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByRole('tab', { name: /突破 10D Mock 第一批/ })).toHaveAttribute('aria-selected', 'false');
  await expect(page.getByText('Mock 状态样本')).toHaveCount(0);
  await expect(page.getByText('状态说明')).toHaveCount(0);
  for (const symbol of ['US.DRAM', 'US.RAM', 'US.SOXX', 'HK.02840']) {
    await expect(page.getByLabel('Kelly 标的状态').getByText(symbol)).toBeVisible();
  }
  for (const symbol of ['US.MSFT', 'US.TSM', 'HK.06951']) {
    await expect(page.getByLabel('Kelly 标的状态').getByText(symbol)).toHaveCount(0);
  }
  await expect(page.getByRole('heading', { name: '趋势回调 20D 第一批' })).toBeVisible();
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
  const symbolStates = page.getByLabel('Kelly 标的状态');
  await expect(symbolStates.getByText('观察中 → 待下单 → 持仓中 → 待退出 → 已完成')).toBeVisible();
  await expect(symbolStates.getByText('该标的在策略监控范围内，但当前没有入场信号，也没有持仓。')).toBeVisible();
  await expect(symbolStates.getByText('入场规则已触发，Kelly 仓位已计算，风控检查已通过。')).toBeVisible();
  await expect(symbolStates.getByText('模拟盘买入已成交，这笔策略样本正在进行中。')).toBeVisible();
  await expect(symbolStates.getByText('这笔持仓已经触发退出规则，但卖出还没有完成。')).toBeVisible();
  await expect(page.getByText('样本不足')).toBeVisible();
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
  await expect(page.getByText('参数推导')).toBeVisible();
  await expect(page.getByText('10 赢 / 8 亏')).toBeVisible();
  await expect(page.getByText('Full Kelly')).toBeVisible();
  await expect(page.getByText('23.1%')).toBeVisible();
  await expect(page.getByText('建议仓位')).toBeVisible();
  await expect(page.getByText('4%', { exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: '突破 10D Mock 第一批' })).toHaveCount(0);
  await page.getByRole('tab', { name: /突破 10D Mock 第一批/ }).click();
  await expect(page.getByRole('tab', { name: /趋势回调 20D 第一批/ })).toHaveAttribute('aria-selected', 'false');
  await expect(page.getByRole('tab', { name: /突破 10D Mock 第一批/ })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByRole('heading', { name: '趋势回调 20D 第一批' })).toHaveCount(0);
  await expect(page.getByRole('heading', { name: '突破 10D Mock 第一批' })).toBeVisible();
  for (const symbol of ['US.MSFT', 'US.TSM', 'HK.06951']) {
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
  await expect(aaplRow.getByRole('button', { name: '凯利' })).toBeVisible();
  await aaplRow.getByRole('button', { name: '凯利' }).click();

  await expect(page.getByRole('heading', { name: /凯利仓位 · US\.AAPL/ })).toBeVisible();
  await expect(page.getByText('阶段 1 不计算 Kelly 仓位', { exact: true })).toBeVisible();
  await expect(page.getByText('趋势回调 20D 第一批').last()).toBeVisible();
});
