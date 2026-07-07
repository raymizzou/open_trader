import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests/e2e',
  timeout: 30_000,
  expect: { timeout: 5_000 },
  use: {
    baseURL: 'http://127.0.0.1:8766',
    trace: 'on-first-retry',
  },
  webServer: {
    command: '.venv/bin/python tests/e2e/serve_dashboard_fixture.py --port 8766',
    url: 'http://127.0.0.1:8766',
    reuseExistingServer: !process.env.CI,
    timeout: 10_000,
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
