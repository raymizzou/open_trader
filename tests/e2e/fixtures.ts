import { test as base, expect, type Page, type Locator } from '@playwright/test';
import { spawn } from 'node:child_process';
import { once } from 'node:events';

export { expect, type Page, type Locator };

export const test = base.extend<{}, { fixtureURL: string }>({
  fixtureURL: [async ({}, use) => {
    const child = spawn(process.env.OPEN_TRADER_PYTHON ?? 'python3',
      ['tests/e2e/serve_dashboard_fixture.py', '--port', '0'],
      { stdio: ['ignore', 'pipe', 'pipe'] });
    let output = '';
    let errors = '';
    child.stderr.on('data', data => { errors += data.toString(); });
    const exited = once(child, 'exit');
    try {
      const url = await new Promise<string>((resolve, reject) => {
        const watchdog = setTimeout(() => reject(new Error(`Fixture startup timed out: ${output}${errors}`)), 10_000);
        child.once('error', error => { clearTimeout(watchdog); reject(error); });
        child.once('exit', (code, signal) => {
          clearTimeout(watchdog);
          reject(new Error(`Fixture exited before startup (${code}/${signal}): ${errors}`));
        });
        child.stdout.on('data', data => {
          output += data.toString();
          const match = output.match(/fixture_dashboard_url: (http:\/\/127\.0\.0\.1:\d+)/);
          if (match) { clearTimeout(watchdog); resolve(match[1]); }
        });
      });
      await use(url);
    } finally {
      if (child.exitCode === null && child.signalCode === null) child.kill('SIGTERM');
      const watchdog = setTimeout(() => child.kill('SIGKILL'), 5_000);
      try { await exited; } finally { clearTimeout(watchdog); }
    }
  }, { scope: 'worker' }],
  baseURL: async ({ fixtureURL }, use) => { await use(fixtureURL); },
});
