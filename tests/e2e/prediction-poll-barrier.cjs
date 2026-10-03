const assert = require('node:assert/strict');

/**
 * Observe one fresh polling cycle after tab-triggered reads have applied.
 * Split mode refreshes Air identity independently of the cloud venues read.
 * @param {import('@playwright/test').Page} page
 * @param {boolean} split
 */
async function waitForPredictionPoll(page, split) {
  const readsApplied = splitMode => !state.predictionMarket.venuesRequestInFlight
    && (!splitMode || !state.predictionMarket.executionRequestInFlight);
  await page.waitForFunction(readsApplied, split);

  // Register both observers together: the same polling tick starts Air first.
  const paths = ['/api/prediction-arbitrage/venues'];
  if (split) paths.push('/api/prediction-arbitrage/execution/identity');
  const requests = await Promise.all(paths.map(path => page.waitForRequest(request =>
    new URL(request.url()).pathname === path)));
  await Promise.all(requests.map(async request => {
    const response = await request.response();
    assert(response, `No response for ${request.url()}`);
    assert(response.ok(), `Polling read failed: ${request.url()}`);
    assert.equal(await response.finished(), null, `Polling read did not finish: ${request.url()}`);
  }));
  // requestfinished precedes response.json(), state updates and synchronous render.
  await page.waitForFunction(readsApplied, split);
}

module.exports = { waitForPredictionPoll };
