import { execFileSync } from 'node:child_process';
import { mkdirSync, unlinkSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import type { FullConfig, FullResult, Reporter, Suite } from '@playwright/test';

const HANDOFF_ENV = 'PREDICTION_ACCEPTANCE_BROWSER_HANDOFF';
const NONCE_ENV = 'PREDICTION_ACCEPTANCE_BROWSER_NONCE';
const REVIEW_ENV = 'PREDICTION_ACCEPTANCE_REVIEW_URL';
const HANDOFF_MAX_AGE_SECONDS = 120;

export default class BrowserHandoffReporter implements Reporter {
  private handoffPath: string | undefined;
  private fixtureUrl: string | undefined;

  onBegin(config: FullConfig, _suite: Suite): void {
    const project = config.projects.find((candidate) => candidate.name === 'chromium') ?? config.projects[0];
    const baseURL = project?.use.baseURL;
    if (typeof baseURL === 'string') {
      this.fixtureUrl = baseURL.replace(/\/$/, '');
    }

    const configuredPath = process.env[HANDOFF_ENV];
    if (!configuredPath) return;

    this.handoffPath = resolve(configuredPath);
    try {
      unlinkSync(this.handoffPath);
    } catch {
      // A missing handoff is the safe state if the run does not pass.
    }
  }

  onEnd(result: FullResult): void {
    if (!this.handoffPath || result.status !== 'passed') return;

    const runNonce = process.env[NONCE_ENV];
    const reviewRaw = process.env[REVIEW_ENV];
    if (!runNonce || !reviewRaw || !this.fixtureUrl) return;

    let candidateCommit: string;
    let review: URL;
    let fixture: URL;
    try {
      candidateCommit = execFileSync(
        'git',
        ['rev-parse', 'HEAD'],
        { cwd: process.cwd(), encoding: 'utf8' },
      ).trim();
      review = new URL(reviewRaw);
      fixture = new URL(this.fixtureUrl);
    } catch {
      return;
    }
    if (!candidateCommit) return;

    const reviewUrl = review.toString().replace(/\/$/, '');
    const healthUrl = new URL('/healthz', review).toString();
    const fixtureUrl = fixture.toString().replace(/\/$/, '');
    const fixtureHealthUrl = new URL('/healthz', fixture).toString();
    const createdAt = Date.now() / 1000;
    const payload = {
      schema_version: 2,
      source: 'playwright',
      playwright_status: 'passed',
      browser_project: 'chromium',
      run_nonce: runNonce,
      fixture_url: fixtureUrl,
      fixture_health_url: fixtureHealthUrl,
      review_url: reviewUrl,
      health_url: healthUrl,
      candidate_commit: candidateCommit,
      created_at: createdAt,
      expires_at: createdAt + HANDOFF_MAX_AGE_SECONDS,
    };

    mkdirSync(dirname(this.handoffPath), { recursive: true });
    writeFileSync(this.handoffPath, JSON.stringify(payload), {
      encoding: 'utf8',
      mode: 0o600,
    });
  }
}
