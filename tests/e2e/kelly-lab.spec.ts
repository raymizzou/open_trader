import { expect, test } from '@playwright/test';

test('renders Kelly lab and opens holding Kelly detail', async ({ page }) => {
  await page.goto('/');

  await expect(page.getByRole('heading', { name: '模拟盘策略实验室' })).toBeVisible();
  await expect(page.getByText('趋势回调 20D 第一批')).toBeVisible();
  await expect(page.getByText('样本不足')).toBeVisible();
  await expect(page.getByText('US.AAPL')).toBeVisible();

  const aaplRow = page.getByRole('row').filter({ hasText: 'AAPL' }).first();
  await expect(aaplRow.getByRole('button', { name: '凯利' })).toBeVisible();
  await aaplRow.getByRole('button', { name: '凯利' }).click();

  await expect(page.getByRole('heading', { name: /凯利仓位 · US\.AAPL/ })).toBeVisible();
  await expect(page.getByText('阶段 1 不计算 Kelly 仓位', { exact: true })).toBeVisible();
  await expect(page.getByText('趋势回调 20D 第一批').last()).toBeVisible();
});
