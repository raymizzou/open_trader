import { expect, test } from '@playwright/test';

test('loads the dashboard and prediction workspace without mutations', async ({ page }) => {
  const unsafeMethods: string[] = [];
  const pageErrors: string[] = [];
  await page.route('**/*', async (route) => {
    const method = route.request().method();
    if (['GET', 'HEAD', 'OPTIONS'].includes(method)) {
      await route.continue();
      return;
    }
    unsafeMethods.push(method);
    await route.abort('blockedbyclient');
  });
  page.on('pageerror', (error) => pageErrors.push(error.message));

  const documentResponse = await page.goto('/', { waitUntil: 'domcontentloaded' });
  expect(documentResponse?.status()).toBe(200);
  await expect(page.locator('#dashboard-shell')).toBeVisible();

  await page.getByRole('button', { name: '预测市场', exact: true }).click();
  await expect(page.locator('#prediction-market-workspace')).toBeVisible();
  await expect(page.getByRole('heading', { name: '预测套利 · 机会' })).toBeVisible();

  const health = await page.evaluate(async () => {
    const response = await fetch('/healthz', { cache: 'no-store' });
    return { ok: response.ok, status: response.status };
  });
  expect(health).toEqual({ ok: true, status: 200 });
  expect(pageErrors).toEqual([]);
  expect(unsafeMethods).toEqual([]);
});
