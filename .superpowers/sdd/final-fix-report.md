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

---

# Dashboard Warm-Ledger Final-Review Fix

Date: 2026-07-16 (Asia/Shanghai)

Base HEAD before fixes: `6e291e6684b223ddf546b043cbf58ebf25ddf484`.

## Commit

- Implementation, tests, and this report: recorded by the commit containing this
  section.

## Fixes

- Corrected all reviewed WCAG AA failures without changing any approved palette
  token. Normal success text on tinted surfaces now uses `var(--text)`; green
  remains the semantic border/marker. Hovered loss text keeps the approved green
  on `var(--surface)` (4.5007:1).
- Narrowed the soft-surface muted override to
  `.trend-stage:not(.cn-trend-stage)`, preserving intentional muted secondary text
  on the main CN report surface, including `.cn-trend-price-sources`.
- Made the desktop CN buy-table scroller keyboard reachable with an accessible
  label and visible approved-token focus ring. Mobile card mode renders
  `tabindex="-1"` and a non-scroll label, so it creates no extra Tab stop.
- Extended permanent Python Playwright acceptance to cover holdings/header/report
  edge alignment, secondary and AA-safe status styles, Kelly Lab, standard
  backtest, decision context, research chat, return paths, and 44px mobile targets
  inside every opened workspace.
- Hardened acceptance fakes so unknown selectors/clicks/expressions fail and real
  navigation, target, geometry, focus, and screenshot paths are recorded.
- Acceptance now removes only the six expected screenshots before a run, records
  the run start, requires the Eastmoney report, accepts a valid zero-buy mobile
  report only with zero buy cards plus `无`, and rejects missing, empty, or stale
  screenshots after browser checks.
- Preserved viewport isolation: a wide-desktop failure still runs desktop and
  mobile; screenshot freshness then reports the missing artifacts from the failed
  viewport.

## Approved Token Check

The exact approved tokens remain unchanged:

```text
#F7F5F1 #FFFEFA #F2EEE7 #201D18 #746E64
#8B5E34 #D8D2C8 #24211D #B42318 #2F855A
```

Adjusted contrast ratios calculated with WCAG relative luminance:

- `#201D18` on `#E7F4EC`: 14.8353:1
- `#201D18` on `#F4FBF7`: 15.9864:1
- `#2F855A` on `#FFFEFA`: 4.5007:1
- `#746E64` on `#FFFEFA`: 5.0058:1

## TDD RED Evidence

- WCAG and selector-contract RED:
  `.venv/bin/python -m pytest tests/test_dashboard_web.py -q -k 'muted_text_meets_aa or success_text_meets_aa or cn_trend_secondary'`
  - `3 failed, 148 deselected in 0.60s`
  - Failures proved the broad `.trend-stage` override, missing hovered-loss safe
    background, and missing adjusted foreground contracts.
- CN scroller accessibility RED:
  `.venv/bin/python -m pytest tests/test_dashboard_web.py -q -k 'cn_buy_scroller'`
  - `1 failed, 151 deselected in 0.43s`
  - The real renderer lacked `tabindex`, `aria-label`, and focus-visible CSS.
- Acceptance-contract RED:
  focused screenshot, navigation, geometry, target-size, zero-buy, unavailable
  report, and visual tests
  - `13 failed, 1 passed, 130 deselected in 0.31s`
  - Expected missing functions/contracts included screenshot naming/freshness,
    workspace navigation, 44px enforcement, holdings geometry, zero-buy behavior,
    and unavailable Eastmoney rejection. One initial test-harness setup error
    (`tmp_path.mkdir()` on an existing fixture directory) was corrected to
    `exist_ok=True` before GREEN work continued.

## GREEN Verification

- Required focused Python tests:
  `.venv/bin/python -m pytest tests/test_dashboard_web.py tests/test_dashboard_acceptance.py -q`
  - `296 passed in 16.83s`
- Required real Chromium E2E:
  `npm run test:e2e -- tests/e2e/dashboard-warm-ledger.spec.ts --project=chromium`
  - `6 passed (2.4s)`
  - Covers computed AA-safe status/hover styles, real tool destinations, desktop
    keyboard focus and accessible label, mobile no-extra-tab-stop behavior, exact
    geometry, and mobile layout.
- Required full Python suite, run once after focused checks passed:
  `.venv/bin/python -m pytest -q`
  - `2178 passed in 28.31s`
- `git diff --check`
  - exit `0`

## Self-Review

- Reviewed every production caller touched by the acceptance flow; no new product
  control, API, dependency, or test entrypoint was added.
- Screenshot cleanup is allow-listed by the exact six names and cannot remove
  unrelated files from `/tmp/open_trader_dashboard_acceptance`.
- Screenshot validation checks exact membership expectations, non-empty size, and
  nanosecond modification time against the current browser run.
- Desktop CN geometry checks both header and holdings edges. The hidden holdings
  panel is exposed only transiently inside the Playwright measurement expression
  and immediately restored.
- Non-empty CN reports retain card/overflow checks. Zero-buy reports skip the
  meaningless desktop overflow requirement and require the mobile empty state.
- Research chat uses a visible production trigger when available; otherwise it
  invokes the existing production `openResearchChat` with the selected real
  holding key and closes through the production button.
- No source-tree screenshot artifacts were created.

## Remaining Risk / Deferred Gate

- No known code-level correctness issue remains.
- Per the final-fix brief, `make acceptance` was intentionally **not** run. Live
  API/data, external report availability, service process state, fresh live logs,
  and review deployment remain for the parent task's final acceptance gate.

---

# Second-Round Final Review Fixes

## Changes

- Kept loss text on the approved `#2F855A` token and added the AA-safe
  `var(--surface)` background whenever a holding row is selected as well as when
  it is hovered. The computed pairing is at least 4.5:1; the unsafe green on the
  selected row's soft background is explicitly covered by a Python contrast test.
- Expanded 375px acceptance coverage to include every visible broker summary card
  and every visible button, input, and select in the open decision workspace,
  including the language toggle. Strict negative tests prove a 43.5px control is
  rejected.
- Added a live media-query change handler for the A-share buy scroller. It now
  resynchronizes `tabindex` and `aria-label` after resize/orientation changes:
  desktop is keyboard-scrollable, while mobile has no extra Tab stop.
- Made the tabbed acceptance fake reject unknown `all_inner_texts`, `evaluate`,
  and `evaluate_all` calls instead of returning plausible empty or overflow data.
- Consolidated the duplicated WCAG luminance calculation in
  `tests/test_dashboard_web.py` into shared helpers.

## TDD RED Evidence

- Active-row contrast and dynamic scroller semantics:
  `.venv/bin/python -m pytest tests/test_dashboard_web.py -q -k 'success_text_meets_aa or cn_buy_scroller_semantics_sync'`
  - `2 failed, 151 deselected in 0.38s`
  - The failures showed the selected-row loss background was missing and the
    breakpoint synchronization helper did not exist.
- Mobile workspace coverage and strict fake behavior:
  `.venv/bin/python -m pytest tests/test_dashboard_acceptance.py -q -k 'opens_real_tool_workspaces or undersized_mobile_target or tabbed_acceptance_fake_rejects'`
  - `2 failed, 2 passed, 142 deselected in 0.15s`
  - The failures showed the production target selectors omitted the new surfaces
    and the fake still returned an empty list for an unknown selector.

## GREEN Verification

- Complete focused Dashboard modules:
  `.venv/bin/python -m pytest tests/test_dashboard_web.py tests/test_dashboard_acceptance.py -q`
  - `299 passed in 17.44s`
- Real Chromium E2E:
  `npx playwright test tests/e2e/dashboard-warm-ledger.spec.ts --project=chromium`
  - `6 passed (2.6s)`
  - Includes computed active-row loss contrast and live 1920px → 375px → 1920px
    scroller semantic synchronization.
- Full Python suite, rerun with an explicit retained summary:
  `zsh -o pipefail -c '.venv/bin/python -m pytest -q | tail -5'`
  - `2181 passed in 28.59s`, exit `0`
- `git diff --check`
  - exit `0`

## Remaining Risk / Deferred Gate

- No known code-level correctness issue remains in this fix scope.
- Per the second-round brief, `make acceptance` was intentionally **not** run.
  Live API/data, background process freshness, logs, and review deployment remain
  for the parent task's final acceptance gate.

---

# Third-Round Important Review Fixes

## Changes

- Added the real `.trend-report-entry button:visible` homepage control to the
  permanent 375px Python acceptance target set and the fixture-backed mobile
  Chromium target set. A strict 43.5px negative case proves that this entry must
  remain at least 44px high.
- Replaced the trend-entry fake's permissive child-selector regex with an exact
  set of known entry, trigger, and button selectors for the four known brokers.
- Removed the fake's arbitrary `strong`, suffix, and substring fallbacks from
  `inner_text()` and related trend/count paths. Known workspace, audit, holding,
  session-price, and CN-row selectors are now exact or anchored and broker/label
  constrained. The review typos `.data-trend-reprot`,
  `.trend-report-entry .misspelled`, and `.totally-wrong strong` all raise.
- The exact approved palette remains unchanged.

## TDD RED Evidence

- Mobile entry coverage and strict fake typo rejection:
  `.venv/bin/python -m pytest tests/test_dashboard_acceptance.py -q -k 'opens_real_tool_workspaces or undersized_mobile_target or tabbed_acceptance_fake_rejects'`
  - `2 failed, 3 passed, 142 deselected in 0.15s`
  - The production mobile selector omitted the report entry, and the fake
    accepted `.data-trend-reprot` as a valid trend-entry child.

## GREEN Verification

- Complete focused Dashboard modules:
  `.venv/bin/python -m pytest tests/test_dashboard_web.py tests/test_dashboard_acceptance.py -q`
  - `300 passed in 16.98s`
- Real Chromium E2E:
  `npx playwright test tests/e2e/dashboard-warm-ledger.spec.ts --project=chromium`
  - `6 passed (2.7s)`
  - The fixture-backed homepage report-entry button passes the 44px target check.
- Full Python suite:
  `.venv/bin/python -m pytest -q`
  - `2182 passed in 28.23s`, exit `0`
- `git diff --check`
  - exit `0`

## Remaining Risk / Deferred Gate

- No known code-level correctness issue remains in this fix scope.
- Per the third-round brief, `make acceptance` was intentionally **not** run.
  Live API/data, background process freshness, logs, and review deployment remain
  for the parent task's final acceptance gate.

---

# Fourth-Round Strict Broker Fake Fix

## Changes

- Added one test-fake broker guard backed by the production-defined four-broker
  fixture order and applied it before every relevant tab/account mutation or
  lookup: tab `count`, `click`, `get_attribute`, account-section `count`, trend
  entry `click`, and disabled-entry inspection.
- Added direct negative coverage proving `futtu` fails in each path,
  `_select_account_tab(page, "futtu")` raises, and the selected broker remains
  unchanged after rejected clicks.
- This is a test-only patch; no product, palette, or E2E code changed.

## TDD RED Evidence

- Unknown broker fail-fast test:
  `.venv/bin/python -m pytest tests/test_dashboard_acceptance.py -q -k 'rejects_unknown_broker_everywhere'`
  - `1 failed, 147 deselected in 0.11s`
  - The unknown tab `count()` incorrectly returned `1` instead of raising.

## GREEN Verification

- Focused permanent Dashboard acceptance:
  `.venv/bin/python -m pytest tests/test_dashboard_acceptance.py -q`
  - `148 passed in 0.46s`
- Full Python suite:
  `.venv/bin/python -m pytest -q`
  - `2183 passed in 32.67s`, exit `0`
- Chromium E2E was not repeated because this patch changes only the Python test
  fake and its negative tests; the prior unchanged E2E result remains `6 passed`.
- `git diff --check`
  - exit `0`

## Remaining Risk / Deferred Gate

- No known code-level correctness issue remains in this test-fake scope.
- Per the fourth-round brief, `make acceptance` was intentionally **not** run.
  Live API/data, background process freshness, logs, and review deployment remain
  for the parent task's final acceptance gate.
