import { defineConfig, devices } from '@playwright/test';

const smokeBaseURL = process.env.OPEN_TRADER_SMOKE_URL;

export default defineConfig({
  testDir: './tests/e2e',
  reporter: process.env.PREDICTION_ACCEPTANCE_BROWSER_HANDOFF
    ? [['./tests/e2e/browser_handoff_reporter.ts']]
    : undefined,
  timeout: 30_000,
  expect: { timeout: 5_000 },
  use: {
    baseURL: smokeBaseURL,
    trace: 'on-first-retry',
  },
  testIgnore: smokeBaseURL ? undefined : /production-smoke\.spec\.ts$/,
  // Local fixtures allocate a worker-owned dynamic listener in fixtures.ts.
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
