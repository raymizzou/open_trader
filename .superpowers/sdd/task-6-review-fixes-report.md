# Task 6 final review fixes

## Outcome

All actionable final-review findings were implemented and verified with focused tests, the complete Dashboard Python suites in scope, and Chromium E2E coverage. `make acceptance` was intentionally not run: the task instructions reserve that final Dashboard gate for the parent agent after integration.

## Finding-by-finding TDD evidence

1. **A broker switch could leave a stale decision deep link.**
   - RED: `test_dashboard_broker_switch_clears_stale_decision_deep_link_before_reload` retained the old `market`, `symbol`, and `decision_tab` query after selecting another broker.
   - GREEN: `selectBroker()` now clears the selected holding and immediately synchronizes the URL before rendering. The test proves a simulated reload does not restore the stale holding.

2. **Numeric leaves were inconsistently grouped.**
   - RED: focused tests exposed ungrouped counts, quantities, decision facts, keyword counts, Bollinger values, technical values, action values, and previous-decision review values.
   - GREEN: known numeric fields now use `formatDisplayNumber`; identifiers, dates, percentages, and prose remain unchanged. Composite technical labels use narrow field-specific formatting in MACD, ATR, support/resistance, and moving-average helpers rather than tokenizing arbitrary text. `suggested_notional` is grouped in both the decision band and action card.

3. **Actual profit/loss values did not consistently show polarity.**
   - RED: `test_dashboard_signed_pnl_formats_signs_groups_and_only_actual_pnl` and `test_dashboard_signed_pnl_covers_tiger_returns_and_kelly_sample_pnl` found unsigned account, backtest, Tiger, and Kelly P/L values.
   - GREEN: actual returns and P/L values show an explicit sign and the established red-profit/green-loss classes. Generic weights and ordinary percentages stay unsigned.
   - Spec follow-up: the API contract represents maximum drawdown as a nonnegative magnitude. The consolidated RED run showed positive/red drawdown in standard backtests, decision evidence, and Tiger metrics. Drawdown now renders as negative, loss-colored risk; negative legacy input is normalized to the same result; zero remains unsigned and neutral.

4. **Broker cards and aliases were incomplete when data was sparse.**
   - RED: `test_dashboard_broker_cards_always_render_four_accounts_and_derive_aliases` and `test_dashboard_empty_payload_keeps_all_broker_cards_and_static_placeholders` found missing accounts and aliases that depended on the first holding row.
   - GREEN: all four configured brokers always render. Aliases can come from broker summaries, cash rows, or broker details. The static Phillips key was corrected and the Eastmoney placeholder was added.

5. **Decision-tab acceptance could search the wrong broker account.**
   - RED: Tiger-only advice-backed fixture coverage could not find its decision button while the default Futu ledger was active.
   - GREEN: `_first_in_scope_holding()` now returns the owning broker and `_check_decision_tabs()` selects that account before locating the exact market/symbol decision button.

6. **Acceptance expectations did not mirror grouped numeric rendering.**
   - RED: trend-stage and audit fixtures using `5000` and `25142.16` no longer matched the grouped UI.
   - GREEN: `_display_number()` is a small independent acceptance oracle used only for known numeric fields. Its implementation intentionally does not call browser code, so the acceptance check can catch UI regressions. This duplication carries a small drift risk and is called out below.

7. **Tabs and controls needed stronger accessibility behavior.**
   - RED: the roving-tab test found no keyboard navigation or valid tab/panel relationships; the initial mobile target check found a 30px segmented control; the old soft surface missed AA contrast for muted text.
   - GREEN: ArrowLeft/ArrowRight/Home/End wrap, select, and focus the intended broker. All four tabs control the single always-present `#account-holdings` tabpanel, whose `aria-labelledby` follows the active tab; only one `.account-section` is rendered. Muted text on the revised soft surface reaches AA contrast. Chromium verifies every tested desktop button/link has a minimum computed dimension of 24px, while the specified mobile controls remain at least 44px. Desktop controls were not inflated to 44px because the approved requirement reserves that size for mobile.

8. **The research-chat E2E was requested to use a real trigger if one exists.**
   - Disposition: the clickable-path premise is not true in the rendered Dashboard detail flow. Production executes `renderAccountTable()` → `renderSymbolDetail()` → `renderTradingDecisionTabs()` → `renderDecisionPlan()`. The only `data-research-chat` button is built by `renderResearchConclusions()` under `renderAnalysisStrategySection()`, which is not called by that production render path.
   - Executable evidence: after Chromium clicks AAPL's real “交易决策” button and the inline detail becomes visible, the E2E asserts `[data-research-chat]` has count `0`. It then calls the existing `openResearchChat()` entry point directly to verify the display-only modal surface. The focused real-browser run passed 3/3 tests.

9. **Dead code and styling remained after earlier workspace changes.**
   - RED: `test_dashboard_static_assets_include_local_shell` was changed to reject unused `closeStandardBacktest`, `closeTrendReport`, and `.cash-detail-panel`; it failed while they existed.
   - GREEN: all three were removed after confirming no production callers or matching DOM.

## Consolidated RED and GREEN checks

The spec self-review additions were first run together before their production fixes:

```text
.venv/bin/python -m pytest -q tests/test_dashboard_web.py -k 'roving_keyboard_and_panel or signed_pnl_formats_signs or signed_pnl_covers_tiger or remaining_numeric_leaves or numeric_suggested_notional'
5 failed, 138 deselected
```

After the fixes, the focused regression command (expanded to cover technical-fact fallbacks) passed:

```text
7 passed, 136 deselected in 0.67s
```

Fresh full verification:

```text
.venv/bin/python -m pytest -q tests/test_dashboard_web.py tests/test_dashboard_acceptance.py
259 passed in 16.33s

OPEN_TRADER_PYTHON=.venv/bin/python npx --no-install playwright test tests/e2e/dashboard-warm-ledger.spec.ts tests/e2e/kelly-lab.spec.ts
6 passed (1.8s)

node --check src/open_trader/dashboard_static/dashboard.js
exit 0

git diff --check
exit 0
```

## Files changed

- `src/open_trader/dashboard_static/dashboard.js`
- `src/open_trader/dashboard_static/dashboard.css`
- `src/open_trader/dashboard_static/index.html`
- `src/open_trader/dashboard_acceptance.py`
- `tests/test_dashboard_web.py`
- `tests/test_dashboard_acceptance.py`
- `tests/e2e/dashboard-warm-ledger.spec.ts`

## Review conclusions and residual concerns

- Standards review: no repository-standard violation remains in the diff. Formatting is applied at known presentation leaves, not by a global text rewriter.
- Spec review: all actionable findings are covered by focused unit/DOM tests and real Chromium flows. The research-trigger finding was rejected based on the rendered runtime DOM, with an executable assertion retained.
- The Python `_display_number()` acceptance oracle intentionally duplicates the JavaScript grouping contract. Keeping it independent improves regression detection, but both implementations must be updated if the display contract changes.
- No live Dashboard acceptance, deployment, background-process restart, or review-URL verification was performed in this task. Those remain for the parent agent's required final `make acceptance` and post-acceptance deployment workflow.

## Final branch re-review addendum

Four additional Important findings were handled in a second TDD cycle.

1. **Grouped CN filter acceptance count**
   - RED: a fake Eastmoney account with 5,000 visible CN rows returned `5,000 条`, while `_check_cn_filter()` expected `5000 条`; the focused run failed 1 test.
   - GREEN: the expectation now uses the existing independent `_display_number(count)` oracle. Both small-count and 5,000-count regressions pass.

2. **Mobile targets across every reachable surface**
   - RED: the real 390px Chromium flow stopped on the standard-backtest `<select>` at 40px. The CSS regression also showed no mobile overrides for backtest controls, decision tabs, or language buttons.
   - GREEN: the mobile-only breakpoint gives backtest inputs/selects, decision tabs, and language-toggle buttons a 44px minimum without changing compact desktop styles. Chromium opens and checks portfolio, Kelly, standard backtest, trend report, and inline decision detail controls. The language-toggle renderer is dormant in the current decision path, so its exact production markup is transparently mounted in the already-open detail panel to verify computed CSS; reachable decision tabs and back/detail buttons are measured normally.

3. **Loading/error tabpanel accessible names**
   - RED: static `#account-holdings` referenced `account-tab-futu` before that tab existed, and loading/error render paths populated tabs and retained `aria-labelledby`.
   - GREEN: initial/loading markup uses `aria-label="账户持仓加载中"`; error state uses `aria-label="账户持仓不可用"` and has no account tabs; a successful render removes the fallback label and assigns `aria-labelledby` to the active, existing tab. The Node DOM regression covers all three states.

4. **T-signal price leaves**
   - RED: latest price, VWAP, and the day range rendered ungrouped values above 999; an already-suffixed percentage could also gain a second `%`.
   - GREEN: field-specific T-signal price helpers group only latest price, VWAP, and day-low/day-high values. The regression preserves `21.13%`, date/timestamp text, and identifier `00001234` unchanged.

Second-cycle focused verification:

```text
tests/test_dashboard_acceptance.py -k 'cn_filter'
2 passed, 115 deselected

tests/test_dashboard_web.py -k 'command_center_css_keeps or tabpanel_uses_fallback or account_tabs_register_roving or t_signal_formats_only_price'
4 passed, 141 deselected

dashboard-warm-ledger.spec.ts -g 'keeps four equal tabs'
1 passed
```

The first full Python run exposed one obsolete test setup that used `dashboardError` merely to skip a large table render. Error state now correctly suppresses account counts, so the fixture was changed to stub the section renderer while exercising a successful 10,000-row account. Fresh final verification after that correction:

```text
.venv/bin/python -m pytest -q tests/test_dashboard_web.py tests/test_dashboard_acceptance.py
262 passed in 16.36s

OPEN_TRADER_PYTHON=.venv/bin/python npx --no-install playwright test tests/e2e/dashboard-warm-ledger.spec.ts tests/e2e/kelly-lab.spec.ts
6 passed (1.7s)

node --check src/open_trader/dashboard_static/dashboard.js
exit 0

git diff --check
exit 0
```

`make acceptance` and live deployment remain intentionally unrun for the parent agent's final gate.
