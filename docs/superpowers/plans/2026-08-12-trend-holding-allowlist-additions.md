# Trend Holding Evidence Allowlist Additions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `US.XLV`, `US.PYPL`, and `HK.06823` to the existing broker-scoped historical-evidence allowlist so current real holdings appear under `趋势持仓` on Account and Trend Report.

**Architecture:** Reuse `_historical_buy_plan_membership()` and its existing browser-side `splitHistoricalTrendHoldings()` consumer without adding an interface or data store. Extend only `TREND_HOLDING_EVIDENCE_ALLOWLIST`; the existing historical scan must still succeed before the matching broker/market allowlist is unioned into the read-only membership contract.

**Tech Stack:** Python 3.12, pytest, existing vanilla JavaScript Dashboard renderer, launchd, Playwright-backed `make acceptance`.

## Global Constraints

- Work only in `/Users/ray/projects/open_trader/.worktrees/trend-holding-sections` on branch `codex/trend-holding-sections`.
- Delegate production/test changes and review fixes to `worker`; after verification, run `reviewer` until it reports no actionable findings.
- Add no dependency, helper, API field, database record, artifact, runtime override, Dashboard editor, or new classification path.
- Classification keys are exactly `("tiger", "US") -> US.XLV, US.PYPL` and `("phillips", "HK") -> HK.06823`.
- Phillips live identity is `name: HKT-SS`, `futu_symbol: HK.06823`; normalized symbol identity, not display name, determines membership.
- Only Account `真实持仓` and Trend Report `盘中持续 -> 已有持仓 -> 真实持仓` classification changes.
- Trading, ordering, historical report contents, simulated holdings, row data, totals, interactions, and current-report row states remain unchanged.
- Preserve scan-first merge and fail-closed behavior: unavailable history returns `available: false`, `symbols: []`, never a partial allowlist.
- Do not run `make acceptance` during development. Run it once after the candidate is clean and reviewer-approved.
- Only `PASS` permits exact-SHA redeployment and user review. Fix `FAIL`; report `BLOCKED` without substitutes.
- After `PASS`, redeploy the exact SHA without source/data edits, then verify fresh PID/cwd/SHA/log/HTTP evidence and both live DOM surfaces for all three symbols.

---

### Task 1: Add the evidence with one minimal TDD cycle

**Files:**
- Modify: `tests/test_dashboard.py:555-611`
- Modify: `tests/test_dashboard_web.py:10109-10210`
- Modify: `src/open_trader/dashboard.py:115-126`
- Modify: `CHANGELOG.md:7-17`

**Interfaces:**
- Consumes: `_historical_buy_plan_membership(reports_dir: Path, *, broker: str, market: str) -> dict[str, object]`, `renderAccountViewPanel(group) -> string`, and `renderTrendHoldingPanel(report, view, items) -> string`.
- Produces: the unchanged `historical_buy_plan_membership` contract with sorted symbols; only the three confirmed symbols are added under their broker/market keys.

- [ ] **Step 1: Expand the backend regression before changing the constant**

Replace `test_historical_buy_plan_membership_adds_tiger_us_evidence_allowlist` with:

```python
@pytest.mark.parametrize(
    ("broker", "market", "expected_symbols"),
    [
        (
            "tiger",
            "US",
            [
                "US.AMZN", "US.CRNX", "US.GRMN", "US.KO", "US.LH",
                "US.NUE", "US.PYPL", "US.REGN", "US.XLV",
            ],
        ),
        ("phillips", "HK", ["HK.06823"]),
    ],
)
def test_historical_buy_plan_membership_adds_scoped_evidence_allowlist(
    tmp_path: Path,
    broker: str,
    market: str,
    expected_symbols: list[str],
) -> None:
    write_buy_plan_history(
        tmp_path, "reports", "report.json", market=market, actions=[]
    )
    membership = dashboard_module._historical_buy_plan_membership(
        tmp_path / "reports", broker=broker, market=market
    )
    assert membership == {
        "available": True,
        "symbols": expected_symbols,
        "reason": "",
    }
```

- [ ] **Step 2: Add one DOM regression for both surfaces and all three identities**

Add after `test_dashboard_shared_historical_trend_holding_split_preserves_rows_and_normalizes_keys`:

```python
def test_dashboard_allowlisted_positions_render_as_trend_on_both_real_surfaces() -> None:
    output = run_dashboard_js(r'''
const tiger={market:"US",real_position_status:"available",historical_buy_plan_membership:{available:true,symbols:["US.XLV","US.PYPL"],reason:""}};
const phillips={market:"HK",real_position_status:"available",historical_buy_plan_membership:{available:true,symbols:["HK.06823"],reason:""}};
state.dashboard={trend_reports:{tiger,phillips}};
state.accountSnapshot={status:"healthy",sources:{account:{brokers:{tiger:{status:"ok"},phillips:{status:"ok"}}}}};
const tigerRows=["XLV","PYPL"].map((symbol,index)=>({
  key:`tiger:US:${symbol}:${index}`,broker:"tiger",
  holding:{market:"US",symbol,futu_symbol:`US.${symbol}`},
  display:{market:"US",symbol,name:symbol,market_value_hkd:"10"},index,
}));
const hkRow={key:"phillips:HK:06823:0",broker:"phillips",
  holding:{market:"HK",symbol:"06823",futu_symbol:"HK.06823",name:"HKT-SS"},
  display:{market:"HK",symbol:"06823",name:"HKT-SS",market_value_hkd:"10"},index:0};
console.log(JSON.stringify({
  accountTiger:renderAccountViewPanel({broker:"tiger",rows:tigerRows}),
  accountPhillips:renderAccountViewPanel({broker:"phillips",rows:[hkRow]}),
  reportTiger:renderTrendHoldingPanel(tiger,"real",[
    {market:"US",symbol:"XLV",name:"XLV"},{market:"US",symbol:"PYPL",name:"PYPL"}]),
  reportPhillips:renderTrendHoldingPanel(phillips,"real",[
    {market:"HK",symbol:"06823",futu_symbol:"HK.06823",name:"HKT-SS"}]),
}));
''')
    rendered = json.loads(output)
    for surface in ("accountTiger", "reportTiger"):
        trend_section = rendered[surface].split("非趋势持仓", 1)[0]
        assert "XLV" in trend_section and "PYPL" in trend_section
    for surface in ("accountPhillips", "reportPhillips"):
        trend_section = rendered[surface].split("非趋势持仓", 1)[0]
        assert "06823" in trend_section and "HKT-SS" in trend_section
```

- [ ] **Step 3: Run the new backend test and confirm RED**

Run:

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_dashboard.py::test_historical_buy_plan_membership_adds_scoped_evidence_allowlist -q
```

Expected: two failures because Tiger lacks `US.PYPL`/`US.XLV`, and Phillips lacks `HK.06823`.

- [ ] **Step 4: Extend only the existing constant**

```python
TREND_HOLDING_EVIDENCE_ALLOWLIST = {
    ("tiger", "US"): frozenset({
        "US.AMZN",
        "US.CRNX",
        "US.GRMN",
        "US.KO",
        "US.LH",
        "US.NUE",
        "US.PYPL",
        "US.REGN",
        "US.XLV",
    }),
    ("phillips", "HK"): frozenset({"HK.06823"}),
}
```

Do not change `_historical_buy_plan_membership()`: `symbols.update(...)` already occurs only after the complete report scan.

- [ ] **Step 5: Run focused classification and DOM tests and confirm GREEN**

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_dashboard.py::test_historical_buy_plan_membership_adds_scoped_evidence_allowlist tests/test_dashboard.py::test_historical_buy_plan_membership_scopes_evidence_allowlist tests/test_dashboard.py::test_historical_buy_plan_membership_does_not_publish_partial_allowlist tests/test_dashboard_web.py::test_dashboard_allowlisted_positions_render_as_trend_on_both_real_surfaces tests/test_dashboard_web.py::test_dashboard_splits_real_account_holdings_by_historical_trend_origin tests/test_dashboard_web.py::test_dashboard_splits_only_real_trend_report_holdings_by_historical_origin -q
```

Expected: PASS; unavailable history still has an empty list and both existing fallback-layout tests stay green.

- [ ] **Step 6: Run the complete Dashboard regression**

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m pytest tests/test_dashboard.py tests/test_dashboard_web.py -q
```

Expected: PASS with no failures.

- [ ] **Step 7: Add the operator-facing CHANGELOG entry**

Under `## 2026-08-12`, add:

```markdown
- 补录 Tiger 美股历史证据缺口 XLV、PYPL 与 Phillips 港股 HK.06823
  （实时账户名称 HKT-SS）：仅改变 Account 与 Trend Report 两处真实持仓的趋势归类；
  交易、下单、模拟持仓和历史报告内容不变；聚焦归类、fail-closed 与两处 DOM 回归通过。
```

- [ ] **Step 8: Check and commit the candidate**

```bash
git diff --check
git status --short
git diff -- src/open_trader/dashboard.py tests/test_dashboard.py tests/test_dashboard_web.py CHANGELOG.md
git add src/open_trader/dashboard.py tests/test_dashboard.py tests/test_dashboard_web.py CHANGELOG.md
git commit -m "fix: classify confirmed trend holdings"
```

Expected: one behavior commit containing only the constant, regressions, and dated operator log.

---

### Task 2: Freeze a clean, reviewer-approved candidate SHA

**Files:**
- Verify only: `src/open_trader/dashboard.py`
- Verify only: `tests/test_dashboard.py`
- Verify only: `tests/test_dashboard_web.py`
- Verify only: `CHANGELOG.md`

**Interfaces:**
- Consumes: Task 1's committed unchanged membership contract.
- Produces: one clean candidate SHA eligible for final acceptance.

- [ ] **Step 1: Delegate review and fix actionable findings**

Ask `reviewer` to compare Task 1 against `docs/superpowers/specs/2026-08-12-trend-holding-allowlist-additions-design.md`, including broker/market isolation, scan-first union, fail-closed behavior, DOM coverage, and non-goals. Send actionable findings to `worker`; after each fix, rerun Task 1 Steps 5-6 and recommit, then rerun `reviewer` until no actionable findings remain.

- [ ] **Step 2: Freeze the candidate identity**

```bash
RELEASE_ROOT=/Users/ray/projects/open_trader/.worktrees/trend-holding-sections
ACCEPTED_SHA="$(git -C "$RELEASE_ROOT" rev-parse HEAD)"
test -n "$ACCEPTED_SHA"
test -z "$(git -C "$RELEASE_ROOT" status --porcelain)"
git -C "$RELEASE_ROOT" diff --check
printf '%s\n' "$ACCEPTED_SHA"
```

Expected: a 40-character SHA, clean worktree, and no diff-check output.

---

### Task 3: Run the final gate and exact-SHA deployment

**Files:**
- Runtime verification only: `logs/frontend_gateway/launchd.out.log`
- Runtime verification only: `logs/legacy_dashboard/launchd.out.log`
- Runtime verification only: `logs/account_api/launchd.out.log`
- Runtime verification only: `data/latest/daily_run_status_{cn,hk,us}.json`

**Interfaces:**
- Consumes: Task 2's clean `ACCEPTED_SHA` and runtime data rooted at `/Users/ray/projects/open_trader`.
- Produces: final `PASS`, exact-SHA runtime evidence, HTTP 200, and live Account/Trend Report DOM proof for all three identities.

- [ ] **Step 1: Install the candidate into acceptance-owned processes**

Run from `$RELEASE_ROOT`:

```bash
RELEASE_ROOT=/Users/ray/projects/open_trader/.worktrees/trend-holding-sections
RUNTIME_ROOT=/Users/ray/projects/open_trader
PYTHON_BIN=/Users/ray/projects/open_trader/.venv/bin/python
ACCEPTED_SHA="$(git -C "$RELEASE_ROOT" rev-parse HEAD)"
cd "$RELEASE_ROOT"
test "$(git rev-parse HEAD)" = "$ACCEPTED_SHA"
test -z "$(git status --porcelain)"
scripts/install_daily_premarket_launchd.sh --config "$RUNTIME_ROOT/config/daily_premarket.env" --trend-only --market all
scripts/install_account_release.sh --repo-root "$RELEASE_ROOT" --runtime-root "$RUNTIME_ROOT" --python "$PYTHON_BIN" --evidence-out "$RUNTIME_ROOT/logs/account_release/trend-holding-allowlist-candidate.json"
scripts/install_dashboard_launchd.sh --mode stack --repo-root "$RELEASE_ROOT" --runtime-root "$RUNTIME_ROOT" --python "$PYTHON_BIN"
```

Expected: 8766/8767/8768 and all three trend controllers run from `$RELEASE_ROOT` at `$ACCEPTED_SHA`.

- [ ] **Step 2: Run `make acceptance` once as the final gate**

```bash
PYTHON_BIN="$PYTHON_BIN" make acceptance
```

Expected: final status exactly `PASS`. On `FAIL`, return to worker/reviewer with a new candidate; on `BLOCKED`, report the blocker and stop without substitutes.

- [ ] **Step 3: Redeploy the exact accepted SHA without source changes**

```bash
test "$(git rev-parse HEAD)" = "$ACCEPTED_SHA"
test -z "$(git status --porcelain)"
scripts/install_daily_premarket_launchd.sh --config "$RUNTIME_ROOT/config/daily_premarket.env" --trend-only --market all
scripts/install_account_release.sh --repo-root "$RELEASE_ROOT" --runtime-root "$RUNTIME_ROOT" --python "$PYTHON_BIN" --evidence-out "$RUNTIME_ROOT/logs/account_release/trend-holding-allowlist-exact-sha.json"
scripts/install_dashboard_launchd.sh --mode stack --repo-root "$RELEASE_ROOT" --runtime-root "$RUNTIME_ROOT" --python "$PYTHON_BIN"
```

Expected: new PIDs at the unchanged `$ACCEPTED_SHA`. Do not rerun acceptance because no source/domain data changed after `PASS`.

- [ ] **Step 4: Verify PID, cwd, SHA, fresh logs, and HTTP 200**

```bash
launchctl print "gui/$(id -u)/com.open-trader.frontend-gateway" | rg 'pid =|state =|last exit code'
launchctl print "gui/$(id -u)/com.open-trader.legacy-dashboard" | rg 'pid =|state =|last exit code'
launchctl print "gui/$(id -u)/com.open-trader.account-api" | rg 'pid =|state =|last exit code'
lsof -nP -iTCP:8766 -sTCP:LISTEN
lsof -nP -iTCP:8767 -sTCP:LISTEN
lsof -nP -iTCP:8768 -sTCP:LISTEN
rg -n 'frontend_gateway_runtime:.*"git_sha":|dashboard_runtime:.*"git_sha":|account_api_runtime:.*"git_sha":' logs/frontend_gateway/launchd.out.log logs/legacy_dashboard/launchd.out.log logs/account_api/launchd.out.log | tail -3
for market in cn hk us; do "$PYTHON_BIN" -c 'import json,pathlib,sys; p=json.loads(pathlib.Path(sys.argv[1]).read_text()); print(sys.argv[1],p["controller"]["pid"],p["controller"]["working_directory"],p["controller"]["git_sha"],p["controller"]["heartbeat_at"])' "$RUNTIME_ROOT/data/latest/daily_run_status_${market}.json"; done
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/
```

Expected: fresh PIDs, `cwd == $RELEASE_ROOT`, `git_sha == $ACCEPTED_SHA`, fresh log/heartbeat timestamps, and `200`.

- [ ] **Step 5: Verify all three identities in both live DOM surfaces**

Use `browser:control-in-app-browser` at `http://127.0.0.1:8766/`:

1. Account `真实持仓`, Tiger: first `.holding-origin-section` headed `趋势持仓` contains exactly one `.account-holding-row[data-broker="tiger"][data-symbol="XLV"]` and one `[data-symbol="PYPL"]`.
2. Account `真实持仓`, Phillips: its first origin section contains exactly one `.account-holding-row[data-broker="phillips"][data-symbol="06823"]`, with visible text `HKT-SS`.
3. Tiger Trend Report, `真实持仓`: first origin section contains one `.cn-trend-card` with `XLV` and one with `PYPL`.
4. Phillips Trend Report, `真实持仓`: first origin section contains one `.cn-trend-card` with both `06823` and `HKT-SS`.
5. Both second origin sections headed `非趋势持仓` contain none of these three rows.

Expected: six positive placements—three symbols on each real-holdings surface—and zero duplicate/non-trend placements. Screenshots are optional and cannot substitute for DOM assertions.
