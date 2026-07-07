import { expect, test } from '@playwright/test';

test('renders Kelly lab and opens holding Kelly detail', async ({ page }) => {
  await page.goto('/');

  await expect(page.getByRole('heading', { name: '持仓列表' })).toBeVisible();
  await expect(page.getByRole('heading', { name: '模拟盘策略实验室' })).toHaveCount(0);
  await expect(page.getByText('趋势回调 20D 第一批')).toHaveCount(0);
  await page.getByRole('button', { name: '凯利实验室' }).click();

  await expect(page.getByRole('heading', { name: '模拟盘策略实验室' })).toBeVisible();
  await expect(page.getByText('趋势回调 20D 第一批')).toBeVisible();
  await expect(page.getByText('样本不足')).toBeVisible();
  await expect(page.getByText('US.AAPL')).toBeVisible();
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
  await page.getByRole('button', { name: '返回主页' }).click();
  await expect(page.getByRole('heading', { name: '模拟盘策略实验室' })).toHaveCount(0);

  const aaplRow = page.getByRole('row').filter({ hasText: 'AAPL' }).first();
  await expect(aaplRow.getByRole('button', { name: '凯利' })).toBeVisible();
  await aaplRow.getByRole('button', { name: '凯利' }).click();

  await expect(page.getByRole('heading', { name: /凯利仓位 · US\.AAPL/ })).toBeVisible();
  await expect(page.getByText('阶段 1 不计算 Kelly 仓位', { exact: true })).toBeVisible();
  await expect(page.getByText('趋势回调 20D 第一批').last()).toBeVisible();
});
