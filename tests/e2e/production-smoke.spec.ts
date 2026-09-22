import { expect, test, type Request } from '@playwright/test';

test('loads the dashboard and prediction workspace without mutations', async ({ page }) => {
  const unsafeMethods: string[] = [];
  const pageErrors: string[] = [];
  const nLegReadRequests: string[] = [];
  const pendingNLegReads = new Set<Request>();
  const isNLegRead = (request: Request) => {
    const path = new URL(request.url()).pathname;
    return path === '/api/prediction-arbitrage/state'
      || path === '/api/prediction-arbitrage/history'
      || path === '/api/prediction-arbitrage/relations'
      || path.startsWith('/api/prediction-arbitrage/relations/')
      || path.startsWith('/api/prediction-arbitrage/n-leg/');
  };
  await page.route('**/*', async (route) => {
    const method = route.request().method();
    if (['GET', 'HEAD', 'OPTIONS'].includes(method)) {
      await route.continue();
      return;
    }
    unsafeMethods.push(method);
    await route.abort('blockedbyclient');
  });
  page.on('request', (request) => {
    if (!isNLegRead(request)) return;
    nLegReadRequests.push(request.url());
    pendingNLegReads.add(request);
  });
  const clearPendingNLegRead = (request: Request) => {
    if (isNLegRead(request)) pendingNLegReads.delete(request);
  };
  page.on('requestfinished', clearPendingNLegRead);
  page.on('requestfailed', clearPendingNLegRead);
  page.on('pageerror', (error) => pageErrors.push(error.message));

  const documentResponse = await page.goto('/', { waitUntil: 'domcontentloaded' });
  expect(documentResponse?.status()).toBe(200);
  await expect(page.locator('#dashboard-shell')).toBeVisible();

  const venuesResponsePromise = page.waitForResponse((response) => (
    new URL(response.url()).pathname === '/api/prediction-arbitrage/venues'
  ));
  await page.getByRole('button', { name: '预测市场', exact: true }).click();
  await expect(page.locator('#prediction-market-workspace')).toBeVisible();
  await expect(page.getByRole('heading', { name: '预测市场' })).toBeVisible();
  const venuesResponse = await venuesResponsePromise;
  expect(venuesResponse.ok()).toBe(true);
  const venuesPayload = await venuesResponse.json() as { n_leg?: { status?: string } };
  const nLegStatus = venuesPayload.n_leg?.status;
  await expect(page.getByRole('tab', { name: 'LP 首页', exact: true })).toHaveAttribute('aria-selected', 'true');
  await expect(page.locator('.pm-venue-card')).toHaveCount(2);
  // #166 起活动 LP 会话下外层面板与内嵌组卡同为 .pm-lp-card，冒烟可见性只锚先出现的外层面板。
  await expect(page.locator('.pm-lp-card').first()).toBeVisible();
  await expect(page.locator('.pm-mode-bar')).toHaveCount(0);

  await page.getByRole('tab', { name: '多腿套利', exact: true }).click();
  await expect(page.getByRole('heading', { name: '多腿套利' })).toBeVisible();
  const nLegPaused = nLegStatus === 'paused';
  if (nLegPaused) {
    await expect(page.getByRole('status').filter({ hasText: '多腿套利已暂停' })).toBeVisible();
    await expect(page.locator('#prediction-market-workspace')).not.toContainText('N_LEG_PAUSED');
    await expect(page.locator('.pm-mode-bar')).toHaveCount(0);
  } else {
    expect(nLegStatus).toBe('running');
    await expect(page.locator('.pm-mode-bar')).toBeVisible();
  }
  await expect(page.getByRole('tab', { name: '多腿套利', exact: true })).toHaveAttribute('aria-selected', 'true');
  await expect(page.locator('.pm-lp-card')).toHaveCount(0);

  if (nLegPaused) {
    await page.getByRole('tab', { name: 'LP 首页', exact: true }).click();
    await expect(page.getByRole('tab', { name: 'LP 首页', exact: true })).toHaveAttribute('aria-selected', 'true');
    // 同上：#166 嵌套同名组卡会使严格模式冲突，只锚外层面板。
    await expect(page.locator('.pm-lp-card').first()).toBeVisible();
    await page.getByRole('tab', { name: '多腿套利', exact: true }).click();
    await expect(page.getByRole('heading', { name: '多腿套利' })).toBeVisible();
    await page.waitForTimeout(6500);
    expect(nLegReadRequests).toEqual([]);
    expect(pendingNLegReads.size).toBe(0);
  }

  const health = await page.evaluate(async () => {
    const response = await fetch('/healthz', { cache: 'no-store' });
    return { ok: response.ok, status: response.status };
  });
  expect(health).toEqual({ ok: true, status: 200 });
  expect(pageErrors).toEqual([]);
  expect(unsafeMethods).toEqual([]);
});
