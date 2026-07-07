import { defineConfig, devices } from '@playwright/test';

const E2E_PORT = 18766;
const E2E_BASE_URL = `http://127.0.0.1:${E2E_PORT}`;
const python = process.env.OPEN_TRADER_PYTHON ?? 'python3';

export default defineConfig({
  testDir: './tests/e2e',
  timeout: 30_000,
  expect: { timeout: 5_000 },
  use: {
    baseURL: E2E_BASE_URL,
    trace: 'on-first-retry',
  },
  webServer: {
    command: `${python} tests/e2e/serve_dashboard_fixture.py --port ${E2E_PORT}`,
    url: E2E_BASE_URL,
    reuseExistingServer: false,
    timeout: 10_000,
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
