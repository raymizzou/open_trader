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

---

# Second Final-Review Fix

Date: 2026-07-12 (Asia/Shanghai)

## Commit

- Implementation and regenerated artifacts: `41c693ccc024e73379839e44cf81c287a830c855`
- Report update: recorded by the commit containing this section.

## Fixes

- Production entry risk now validates current templates/experiments without
  strategy stats or optional order artifacts, derives the exact configured
  experiment IDs, and requires unified stats to cover that set exactly.
- Invalid config or stats blocks every entry with a
  `strategy_stats_provenance` failure while valid exits continue to approval.
- Malformed optional paper-order/execution artifacts cannot affect entry
  provenance authorization; malformed experiment configuration still fails
  closed.
- Pending-entry lifecycle and intent narratives now say only that the entry rule
  triggered and sizing/risk checks are pending. Intent generation overwrites
  stale persisted pending-entry narratives.
- Checked-in lifecycle, intent, risk, execution, sample, and strategy-stat
  artifacts were regenerated as one chain. The entry remains `0%` and blocked;
  the exit remains approved.

## TDD Evidence

- Exact-coverage production RED:
  `PYTHONPATH=src .venv/bin/python -m pytest tests/test_kelly_order_risk.py::test_production_risk_blocks_entry_when_stats_omit_configured_experiment tests/test_kelly_order_risk.py::test_production_risk_blocks_entry_when_experiment_config_is_malformed -q`
  - `2 failed in 0.29s`
- Exact-coverage production GREEN:
  `PYTHONPATH=src .venv/bin/python -m pytest tests/test_kelly_order_risk.py -q`
  - `34 passed in 0.22s`
- Narrative RED: focused lifecycle, intent, and dashboard tests
  - `3 failed in 0.30s`
- Narrative lifecycle/intent GREEN
  - `2 passed in 0.01s`
- Combined focused GREEN:
  `PYTHONPATH=src .venv/bin/python -m pytest tests/test_kelly_lifecycle.py tests/test_kelly_order_intents.py tests/test_kelly_order_risk.py tests/test_dashboard_web.py::test_dashboard_js_renders_kelly_lab_panel -q`
  - `52 passed in 0.47s`
- Checked-in consistency RED before regeneration
  - `1 failed` because the old intent still claimed `4%` and `风控通过`
- Checked-in consistency GREEN after regeneration
  - `1 passed in 0.01s`
- Optional operational-artifact isolation RED:
  `PYTHONPATH=src .venv/bin/python -m pytest tests/test_kelly_order_risk.py::test_production_risk_ignores_malformed_operational_artifacts -q`
  - `2 failed in 0.06s`
- Optional operational-artifact isolation GREEN:
  `PYTHONPATH=src .venv/bin/python -m pytest tests/test_kelly_order_risk.py tests/test_kelly_lab.py -q`
  - `69 passed in 0.27s`

## Real CLI Chain

All commands exited `0`:

```text
PYTHONPATH=src .venv/bin/python -m open_trader kelly build-trade-samples --data-dir data --generated-at '2026-07-12 00:35'
samples: 0; open_positions: 0; skipped_orders: 0

PYTHONPATH=src .venv/bin/python -m open_trader kelly build-strategy-stats --data-dir data --generated-at '2026-07-12 00:35'
experiments: 3

PYTHONPATH=src .venv/bin/python -m open_trader kelly build-order-intents --data-dir data --created-at '2026-07-12 00:35'
intents: 2

PYTHONPATH=src .venv/bin/python -m open_trader kelly check-order-risk --data-dir data --checked-at '2026-07-12 00:35'
intents: 2; approved: 1; blocked: 1

PYTHONPATH=src .venv/bin/python -m open_trader kelly execute-orders --data-dir data --dry-run --executed-at '2026-07-12 00:35' --limit-price HK.02840=2950 --order-qty HK.02840=1
executions: 2; dry_run: 1; submitted: 0; skipped: 1; failed: 0
```

Generated evidence digest:
`703cd9842e55547f5b68f4e4d710baecdc23906221a73dfc610c0a4da055d815`.

## Final Verification

- `PYTHONPATH=src .venv/bin/python -m pytest -q`
  - `1247 passed in 16.68s`
- `npm run test:e2e:kelly`
  - `3 passed (1.3s)`
- `PYTHONPATH=src .venv/bin/python -m compileall -q src tests`
  - exit `0`
- `git diff --check`
  - exit `0`
- First `codex review --uncommitted`
  - identified optional order-artifact coupling; fixed with a RED/GREEN
    production regression.
- Second `codex review --uncommitted`
  - `No actionable correctness issues were identified.`

## Live Verification

- Stopped only the prior `open_trader_dashboard_8766` screen/PID `42841` and
  preserved the unrelated watch and Xiaozhi screens.
- Restarted `open_trader_dashboard_8766` from this feature worktree.
- Dashboard Python PID: `49008`; start: `Sun Jul 12 00:38:20 2026`.
- Fresh log `/tmp/open_trader_dashboard_8766.log` contains
  `dashboard_url: http://127.0.0.1:8766`.
- Live `/api/dashboard`: Kelly available, 3 experiments, canonical pending-entry
  reason/action, with no pre-risk percentage or approval claim.
- Live Playwright: canonical pending narrative rendered twice; legacy `4%` claim
  absent; `风控通过` absent.
- Checked-in risk API source artifact: 2 checks, entry blocked, exit approved.

## Concerns

- No unresolved correctness concerns.
- The E2E narrative update initially exposed a strict-locator duplicate because
  the same canonical text intentionally appears in reason and meaning; the test
  now asserts an exact count of 2 and both rendered values.
- Pre-existing untracked `.venv` and `node_modules` symlinks were preserved.

# Trading Decision Tabs Final Review Fix — 2026-07-13

## Fix

- Commit: `eb442b8` (`fix: enable Futu news decision tab`).
- Files: `src/open_trader/dashboard_static/dashboard.js`, `tests/test_dashboard_web.py`.
- News availability now accepts either `decision_facts.news_sentiment` or the existing
  `futuSkillNewsSentimentModule(holding)` result; errors prefer the decision source,
  then Futu, before the existing `数据未生成` fallback.
- Replaced the stale placeholder claim with concise copy describing connected decision
  and market-fact data. No arrow-key or roving-tabindex behavior was added.

## RED / GREEN

- RED: `.venv/bin/python -m pytest -q tests/test_dashboard_web.py::test_dashboard_news_tab_uses_futu_skill_news_sentiment`
  failed as expected because the News button contained `decision-tab-failed`.
- GREEN: the new regression plus the existing tab test: `2 passed in 0.27s`.
- Focused final coverage (new regression, existing tabs, static copy): `3 passed in 0.38s`.
- The first full run found the intentionally stale copy assertion: `1 failed, 1447 passed
  in 20.92s`; after correcting it, `make test` returned `1448 passed in 20.13s`.

## Acceptance / Live Evidence

- Final `make acceptance`: tests `1448 passed in 20.21s`; exact gate result:
  `{"status": "PASS", "pid": 13895, "errors": [], "blocker": null}`.
- Live PID `13895`, started `Mon Jul 13 11:35:22 2026`, runs
  `python -m open_trader dashboard ... --port 8766` from the feature worktree.
- The PASS gate verified real API/data, two refresh cycles, process version, fresh logs,
  and desktop/mobile browser flows; no acceptance errors or blockers were reported.

## Self-review / Concerns

- Diff is limited to the shared tab-definition path, one runtime regression, and the
  stale copy assertion; `git diff --check` exited `0` before commit.
- Real domestic-discussion rendering continues through the existing plugin/helper path.
- No unresolved concerns. The pre-existing untracked `.venv` symlink was not added.
