# Kelly Strategy Stats Final-Fix Report

Date: 2026-07-12 (Asia/Shanghai)

## Commits

- Implementation: `293207a06f0b28efc93d402e62f5248c453ba275`
- Report: recorded by the commit containing this file.

## Fixes

- Order-intent generation first loads unified strategy stats. Missing, malformed,
  stale, or experiment-incomplete strategy stats trigger a validated
  `include_strategy_stats=False` reload that emits pending exits only. Template,
  experiment, trade-sample, paper-order, and lifecycle errors still propagate.
- Exit intents no longer carry or require entry sizing/provenance. Production risk
  approves valid exits even when strategy-stat artifacts are unavailable.
- Entry risk loads current validated trade samples and unified strategy stats, then
  requires exact equality for position percentage, parameter source, stats time,
  source-sample time, and evidence digest. Missing/legacy/mismatched provenance
  blocks the entry.
- Strategy stats contain a canonical SHA-256 of trade evidence at top level and in
  every experiment record. The digest excludes compatibility
  `stats_by_experiment` and detects same-minute evidence changes.
- Stats validation now requires classified samples to equal completed samples and
  both record timestamps to equal top-level `generated_at`.
- Trade-sample percentages again use the base branch's two-decimal `_pct_text`
  serialization, including the `+/-0.005%` half-up boundary.
- Blank and duplicate configured experiment IDs are rejected explicitly.
- Checked-in trade samples, strategy stats, intents, risk checks, and dry-run
  executions were regenerated as one consistent chain.

## TDD Evidence

- Baseline: `PYTHONPATH=src .venv/bin/python -m pytest -q`
  - `1204 passed in 17.41s`
- Digest/validator/precision/ID RED:
  `PYTHONPATH=src .venv/bin/python -m pytest tests/test_kelly_strategy_stats.py tests/test_kelly_trade_samples.py tests/test_kelly_lab.py -q`
  - `13 failed, 75 passed in 0.15s`
- Digest/validator/precision/ID GREEN (checked-in artifact deferred):
  same focused command with
  `-k 'not test_load_checked_in_kelly_data_uses_unified_strategy_stats'`
  - `88 passed, 1 deselected in 0.08s`
- Same-minute Kelly Lab digest RED:
  `PYTHONPATH=src .venv/bin/python -m pytest tests/test_kelly_lab.py::test_lab_rejects_changed_trade_evidence_with_same_generated_minute -q`
  - `1 failed`
- Exit fallback/provenance RED:
  focused intent and risk cases
  - `16 failed, 6 passed in 0.11s`
- Intent/risk GREEN:
  `PYTHONPATH=src .venv/bin/python -m pytest tests/test_kelly_order_intents.py tests/test_kelly_order_risk.py -q`
  - `40 passed in 0.05s`
- Production exit independence RED:
  focused production risk tests
  - `1 failed, 1 passed in 0.06s`
- Builder/CLI/risk GREEN:
  `PYTHONPATH=src .venv/bin/python -m pytest tests/test_kelly_order_risk.py tests/test_kelly_order_intents.py tests/test_kelly_order_intents_cli.py -q`
  - `48 passed in 0.21s`

## Real CLI Chain

All commands exited `0`:

```text
PYTHONPATH=src .venv/bin/python -m open_trader kelly build-trade-samples --data-dir data --generated-at '2026-07-12 00:18'
samples: 0; open_positions: 0; skipped_orders: 0

PYTHONPATH=src .venv/bin/python -m open_trader kelly build-strategy-stats --data-dir data --generated-at '2026-07-12 00:18'
experiments: 3

PYTHONPATH=src .venv/bin/python -m open_trader kelly build-order-intents --data-dir data --created-at '2026-07-12 00:18'
intents: 2

PYTHONPATH=src .venv/bin/python -m open_trader kelly check-order-risk --data-dir data --checked-at '2026-07-12 00:18'
intents: 2; approved: 1; blocked: 1

PYTHONPATH=src .venv/bin/python -m open_trader kelly execute-orders --data-dir data --dry-run --executed-at '2026-07-12 00:18' --limit-price HK.02840=2950 --order-qty HK.02840=1
executions: 2; dry_run: 1; submitted: 0; skipped: 1; failed: 0
```

Generated evidence digest:
`7ebe55511417264974b7c525948d82d87b6700ec2847e000f1501dd095127111`.
The zero-sample entry is `0%` and blocked; the pending exit is approved.

## Final Verification

- `PYTHONPATH=src .venv/bin/python -m pytest -q`
  - `1243 passed in 17.03s`
- `npm run test:e2e:kelly`
  - `3 passed (1.7s)`
- `PYTHONPATH=src .venv/bin/python -m compileall -q src tests`
  - exit `0`
- `git diff --check`
  - exit `0`
- `codex review --uncommitted`
  - `No actionable defects were identified in the changed code.`
  - Its unrestricted-relevant review completed; its attempted full suite had
    sandbox-only socket and external Futu-log permission failures. The full suite
    above ran outside that reviewer sandbox and passed.

## Live Verification

- Inspected `screen`, `launchctl`, and process state. The old dashboard from the
  main checkout was stopped; unrelated `open_trader_watch_t_HK` and Xiaozhi
  screens were preserved.
- Restarted `open_trader_dashboard_8766` from this feature worktree with fresh log
  `/tmp/open_trader_dashboard_8766.log`.
- Dashboard Python PID: `42841`; start: `Sun Jul 12 00:22:13 2026`.
- Log contains `dashboard_url: http://127.0.0.1:8766` and fresh Futu connection
  timestamps beginning `2026-07-12 00:22:17`.
- Live `/api/dashboard`: Kelly available, 3 experiments, all records use the
  generated digest and `0%` suggested position.
- Live Playwright: rendered `趋势回调 20D Mock US 第一批`, position `0%`, stats
  time `2026-07-12 00:18`.

## Concerns

- No unresolved correctness concerns.
- Pre-existing untracked `.venv` and `node_modules` symlinks were not modified or
  committed.
