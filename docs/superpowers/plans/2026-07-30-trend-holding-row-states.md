# Trend Holding Row States Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sort every CN/HK/US real and simulated holding table by report strength and color each row as trend-report, non-trend-report, or blacklisted without changing the existing table contract.

**Architecture:** Keep frozen reports unchanged and compute a small `trend_report_state` display field in the existing Dashboard projection. Reuse the existing market-aware symbol conversion and individual strength key, then let the shared holding renderer add one fixed CSS class to each row.

**Tech Stack:** Python 3.12, pytest, static JavaScript/CSS, Node VM renderer checks, existing Dashboard acceptance.

## Global Constraints

- Apply identical behavior to CN, HK, and US, in both `真实持仓` and `模拟盘持仓`.
- Membership is only the current projected `buy_actions + hold_actions` symbol union.
- Sort by numeric `strength` descending; missing strength sorts last; blacklisted rows sort last.
- Keep the existing section, tabs, ten columns, values, typography, spacing, option-anomaly behavior, desktop table, and mobile card layout.
- Use existing backgrounds: trend `#e7f4ec`, non-trend `#fae8e6`, blacklist `var(--surface-soft)`.
- Add no label, badge, legend, column, dependency, frozen-report field, strategy change, notification change, or execution change.
- Run `make acceptance` only after source, tests, changelog, and live checks are final.

---

### Task 1: Project membership state and strength-first order

**Files:**
- Modify: `src/open_trader/dashboard.py:956-960, 1025-1148, 1206-1240, 1243-1289, 2150-2154`
- Modify: `tests/test_dashboard.py:466-524, 2528-2629`

**Interfaces:**
- Consumes: projected `buy_actions`, `hold_actions`, and `real_position_actions`; existing `to_futu_symbol()` and `_project_trend_holding_individual_key()`.
- Produces: `_canonical_trend_symbol(item, market) -> str`, `_project_trend_membership_state(item, *, market, included_symbols) -> str`, and `trend_report_state` values `included`, `excluded`, or `blacklisted`.

- [ ] **Step 1: Write the failing market-normalization and state test**

Add next to the existing real-position projection test:

```python
@pytest.mark.parametrize(
    ("market", "report_symbol", "holding_symbol"),
    [
        ("CN", "600000", "SH.600000"),
        ("HK", "700", "HK.00700"),
        ("US", "BRK.B", "US.BRK.B"),
    ],
)
def test_trend_holding_membership_state_is_market_aware(
    market: str,
    report_symbol: str,
    holding_symbol: str,
) -> None:
    included_symbols = {
        dashboard_module._canonical_trend_symbol(
            {"symbol": report_symbol}, market
        )
    }

    assert dashboard_module._project_trend_membership_state(
        {"symbol": holding_symbol},
        market=market,
        included_symbols=included_symbols,
    ) == "included"
    assert dashboard_module._project_trend_membership_state(
        {"symbol": "INVALID"},
        market=market,
        included_symbols=included_symbols,
    ) == "excluded"
    assert dashboard_module._project_trend_membership_state(
        {"symbol": holding_symbol, "reason": "holding_trend_excluded"},
        market=market,
        included_symbols=included_symbols,
    ) == "blacklisted"
```

- [ ] **Step 2: Change the existing real/sim projection fixture to cover order and all three states**

In `test_trend_report_projects_frozen_real_positions_separately_from_simulation`,
set:

```python
judgments.update({
    "holding_decisions": [
        {
            "action": "HOLD",
            "symbol": "SPY",
            "name": "标普ETF",
            "reason": "trend_intact",
            "strength": "50",
        },
    ],
    "real_holding_decisions_status": "available",
    "real_holding_decisions_source": {
        "broker": "tiger", "broker_label": "老虎",
        "snapshot_period": "2026-07-15", "source_kind": "statement",
        "freshness_text": "非实时", "read_only_text": "只读，不自动下单",
    },
    "real_holding_decisions": [
        {
            "action": "HOLD", "symbol": "SPY", "name": "标普ETF",
            "reason": "trend_intact", "strength": "50",
        },
        {
            "action": "SELL_ALL", "symbol": "VIXY", "name": "波动率ETF",
            "reason": "danger_signal", "strength": "20",
        },
        {
            "action": "MANUAL_REVIEW", "symbol": "QQQ", "name": "纳指ETF",
            "reason": "holding_signal_unknown", "strength": "90",
        },
        {
            "action": "MANUAL_REVIEW", "symbol": "EUV", "name": "EUV",
            "reason": "holding_signal_unknown", "strength": None,
        },
        {
            "action": "MANUAL_REVIEW", "symbol": "US.AGRZ", "name": "AGRZ",
            "reason": "holding_trend_excluded", "strength": "99",
        },
    ],
})
```

Replace the order/count assertions with:

```python
assert [item["symbol"] for item in report["real_position_actions"]] == [
    "QQQ", "SPY", "VIXY", "EUV", "US.AGRZ",
]
assert {
    item["symbol"]: item["trend_report_state"]
    for item in report["real_position_actions"]
} == {
    "QQQ": "excluded",
    "SPY": "included",
    "VIXY": "included",
    "EUV": "excluded",
    "US.AGRZ": "blacklisted",
}
assert report["hold_actions"][0]["trend_report_state"] == "included"
assert report["counts"] == {"sell": 0, "buy": 1, "hold": 1, "review": 0}
```

- [ ] **Step 3: Replace the obsolete industry-order expectation**

Rename `test_dashboard_projects_holdings_with_frozen_industry_order` to
`test_dashboard_projects_holdings_in_strength_order`, keep its CN/HK/US
parameterization and frozen-field assertions, and change:

```python
assert [item["symbol"] for item in holds] == ["MED", "FIN"]
assert holds[0]["industry"] == "医疗保健"
assert holds[0]["industry_tm_id"] == 1
assert holds[0]["days"] == 7
```

Delete `test_dashboard_falls_back_to_individual_holding_order_for_invalid_context`;
the industry context no longer participates in this table's order.

- [ ] **Step 4: Run the focused tests to verify RED**

Run:

```bash
PYTHONPATH=.:src .venv/bin/python -m pytest -q \
  tests/test_dashboard.py::test_trend_holding_membership_state_is_market_aware \
  tests/test_dashboard.py::test_trend_report_projects_frozen_real_positions_separately_from_simulation \
  tests/test_dashboard.py::test_dashboard_projects_holdings_in_strength_order
```

Expected: failures because the membership helpers/state do not exist and the
current real/sim sort still uses urgency/industry context.

- [ ] **Step 5: Implement the minimum shared projection**

Rename `_canonical_trend_sell_symbol` to `_canonical_trend_symbol`, update its
existing sell-action callers, and add:

```python
def _project_trend_membership_state(
    item: dict[str, Any],
    *,
    market: str,
    included_symbols: set[str],
) -> str:
    if item.get("reason") == "holding_trend_excluded":
        return "blacklisted"
    symbol = _canonical_trend_symbol(item, market)
    return "included" if symbol and symbol in included_symbols else "excluded"
```

Replace the simulated hold sort with:

```python
hold_actions = sorted(
    [
        item
        for item in holdings
        if item.get("action") == "HOLD"
        and not _trend_action_needs_review(item)
    ],
    key=_project_trend_holding_individual_key,
)
```

Replace the real-position urgency sort with:

```python
return sorted(
    projected_items,
    key=lambda item: (
        item.get("reason") == "holding_trend_excluded",
        *_project_trend_holding_individual_key(item),
    ),
)
```

After `_project_trend_actions()` and `_project_trend_real_actions()` return in
`_project_broker_trend_report()`, attach the display state:

```python
included_symbols = {
    symbol
    for item in [*buy_actions, *hold_actions]
    if (symbol := _canonical_trend_symbol(item, market))
}
for item in [*hold_actions, *real_position_actions]:
    item["trend_report_state"] = _project_trend_membership_state(
        item,
        market=market,
        included_symbols=included_symbols,
    )
```

Delete the now-unused industry-order helpers
`_project_trend_holding_context`,
`_project_trend_valid_holding_context`,
`_project_trend_holding_has_history`,
`_project_trend_holding_context_key`, and
`_project_trend_sorted_holdings`, plus their now-unused ordering imports.
Keep frozen snapshot enrichment unchanged.

- [ ] **Step 6: Run the focused tests to verify GREEN**

Run the Step 4 command again.

Expected: all selected parameter cases pass.

- [ ] **Step 7: Commit the projection change**

```bash
git add src/open_trader/dashboard.py tests/test_dashboard.py
git commit -m "feat: classify and sort trend holding rows"
```

### Task 2: Render the three row backgrounds

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js:3594-3627`
- Modify: `src/open_trader/dashboard_static/dashboard.css:2656-2677`
- Modify: `tests/test_dashboard_web.py:7086-7125`

**Interfaces:**
- Consumes: `item.trend_report_state` from Task 1.
- Produces: row classes `trend-holding-included`, `trend-holding-excluded`, and `trend-holding-blacklisted`.

- [ ] **Step 1: Expand the renderer regression**

Change both `hold_actions` and `real_position_actions` in
`test_dashboard_renders_real_and_simulated_trend_holding_tabs` so each tab has
one row for every state:

```javascript
hold_actions:[
  {action:"HOLD",symbol:"SIM-IN",name:"模拟趋势",reason:"trend_intact",trend_report_state:"included"},
  {action:"HOLD",symbol:"SIM-OUT",name:"模拟非趋势",reason:"trend_intact",trend_report_state:"excluded"},
  {action:"MANUAL_REVIEW",symbol:"SIM-BLACK",name:"模拟黑名单",reason:"holding_trend_excluded",trend_report_state:"blacklisted"},
],
real_position_actions:[
  {action:"HOLD",symbol:"REAL-IN",name:"真实趋势",reason:"trend_intact",trend_report_state:"included"},
  {action:"MANUAL_REVIEW",symbol:"REAL-OUT",name:"真实非趋势",reason:"holding_signal_unknown",trend_report_state:"excluded"},
  {
    action:"MANUAL_REVIEW",symbol:"US.AGRZ",name:"AGRZ",
    reason:"holding_trend_excluded",trend_report_state:"blacklisted",
    temperature_prev:null,temperature_curr:null,phase:null,strength:null,
    industry:"",close:null,active_line:null,
  },
],
```

Inside the existing CN/HK/US loop, add:

```javascript
for (const state of ["included", "excluded", "blacklisted"]) {
  const rows = holding.match(new RegExp(`class="cn-trend-card trend-holding-${state}"`, "g")) || [];
  if (rows.length !== 2) throw new Error(`${market}:${state}:${holding}`);
}
if (holding.includes("非趋势报告标的")) throw new Error(holding);
const agrz = (holding.match(/<tr class="cn-trend-card[^"]*">[\s\S]*?<\/tr>/g) || [])
  .find((row) => row.includes("US.AGRZ")) || "";
```

After the Node assertion, add CSS checks:

```python
css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")
assert ".trend-holding-included td" in css
assert "background: #e7f4ec;" in css
assert ".trend-holding-excluded td" in css
assert "background: #fae8e6;" in css
assert ".trend-holding-blacklisted td" in css
assert "background: var(--surface-soft);" in css
```

- [ ] **Step 2: Run the renderer test to verify RED**

Run:

```bash
PYTHONPATH=.:src .venv/bin/python -m pytest -q \
  tests/test_dashboard_web.py::test_dashboard_renders_real_and_simulated_trend_holding_tabs
```

Expected: failure because the renderer emits no state classes and the CSS rules
do not exist.

- [ ] **Step 3: Add the row-class mapping**

Add before `renderTrendHoldingRows()`:

```javascript
function trendHoldingRowClass(item) {
  return {
    included: "trend-holding-included",
    excluded: "trend-holding-excluded",
    blacklisted: "trend-holding-blacklisted",
  }[item?.trend_report_state] || "trend-holding-excluded";
}
```

Change only the opening row tag:

```javascript
return cnTrendRows(items).map((item) => `<tr class="cn-trend-card ${trendHoldingRowClass(item)}">
```

- [ ] **Step 4: Add the three existing-theme backgrounds**

Add after the shared trend-table cell rules:

```css
.cn-trend-table .cn-trend-card.trend-holding-included td {
  background: #e7f4ec;
}

.cn-trend-table .cn-trend-card.trend-holding-excluded td {
  background: #fae8e6;
}

.cn-trend-table .cn-trend-card.trend-holding-blacklisted td {
  background: var(--surface-soft);
}
```

The selector intentionally outranks the existing mobile
`.cn-trend-card td:nth-child(-n + 2)` rule so the whole mobile card uses one
background.

- [ ] **Step 5: Run the renderer test to verify GREEN**

Run the Step 2 command again.

Expected: one passing test covering both tabs in CN, HK, and US.

- [ ] **Step 6: Commit the renderer change**

```bash
git add \
  src/open_trader/dashboard_static/dashboard.js \
  src/open_trader/dashboard_static/dashboard.css \
  tests/test_dashboard_web.py
git commit -m "feat: color trend holding row states"
```

### Task 3: Log, verify, accept, and deploy the review build

**Files:**
- Modify: `CHANGELOG.md`
- Runtime evidence only: `/tmp/open_trader_trend_preview_18766.log`

**Interfaces:**
- Consumes: committed Tasks 1-2 and the existing read-only CN/HK/US runtime report data.
- Produces: a fully accepted feature-branch SHA and a fresh review Dashboard on port `18766`.

- [ ] **Step 1: Add the operator-facing changelog entry**

Under `## 2026-07-30`, add:

```markdown
- Sorted CN/HK/US real and simulated trend-report holding rows by report
  strength. Rows now reuse the existing light green, light pink, and soft gray
  backgrounds to distinguish current buy/hold membership, non-trend holdings,
  and trend-lookup blacklist exclusions without changing the ten-column table,
  strategy, execution, or Feishu output.
```

- [ ] **Step 2: Run the affected suites and formatting check**

Run:

```bash
PYTHONPATH=.:src .venv/bin/python -m pytest -q \
  tests/test_dashboard.py tests/test_dashboard_web.py
git diff --check
```

Expected: exit 0, zero failures, and no whitespace errors.

- [ ] **Step 3: Commit the changelog before any merge**

```bash
git add CHANGELOG.md
git commit -m "docs: log trend holding row states"
git status --short --branch
```

Expected: clean `feat/trend-holding-row-states`.

- [ ] **Step 4: Start the exact branch SHA against the existing read-only preview data**

Stop the old preview process, clear its log, and start the committed worktree:

```bash
screen -S open_trader_trend_preview -X quit || true
truncate -s 0 /tmp/open_trader_trend_preview_18766.log
screen -dmS open_trader_trend_preview zsh -lc \
  'cd /Users/ray/projects/open_trader/.worktrees/trend-holding-row-states && exec env PYTHONPATH=src .venv/bin/python -u -m open_trader dashboard --host 127.0.0.1 --port 18766 --portfolio /Users/ray/Library/Caches/open_trader/trend-real-holdings-preview-20260730/data/latest/portfolio.csv --data-dir /Users/ray/Library/Caches/open_trader/trend-real-holdings-preview-20260730/data --reports-dir /Users/ray/Library/Caches/open_trader/trend-real-holdings-preview-20260730/reports --config /Users/ray/projects/open_trader/config/daily_premarket.env --prediction-config /Users/ray/projects/open_trader/config/prediction_arbitrage.json >> /tmp/open_trader_trend_preview_18766.log 2>&1'
curl --fail --retry 20 --retry-delay 1 http://127.0.0.1:18766/
```

- [ ] **Step 5: Check the live CN/HK/US API contract directly**

Run:

```bash
.venv/bin/python - <<'PY'
from decimal import Decimal
import json
from urllib.request import urlopen

payload = json.load(urlopen("http://127.0.0.1:18766/api/dashboard", timeout=10))
reports = payload["trend_reports"]
for broker, market in (("eastmoney", "CN"), ("phillips", "HK"), ("tiger", "US")):
    report = reports[broker]
    assert report["market"] == market
    for key in ("hold_actions", "real_position_actions"):
        rows = report[key]
        assert all(
            row["trend_report_state"] in {"included", "excluded", "blacklisted"}
            for row in rows
        )
        numeric = [
            Decimal(str(row["strength"]))
            for row in rows
            if row["trend_report_state"] != "blacklisted"
            and row.get("strength") not in (None, "")
        ]
        assert numeric == sorted(numeric, reverse=True)
        seen_missing = False
        for row in (
            row for row in rows
            if row["trend_report_state"] != "blacklisted"
        ):
            if row.get("strength") in (None, ""):
                seen_missing = True
            else:
                assert not seen_missing
        blacklisted = [
            index for index, row in enumerate(rows)
            if row["trend_report_state"] == "blacklisted"
        ]
        assert not blacklisted or blacklisted == list(range(blacklisted[0], len(rows)))
print("CN/HK/US holding states and strength order: PASS")
PY
```

Expected: the printed `PASS` line. No report file or execution ledger changes.

- [ ] **Step 6: Run the final Dashboard acceptance gate**

Run only now:

```bash
DASHBOARD_URL=http://127.0.0.1:18766 \
DASHBOARD_LOG=/tmp/open_trader_trend_preview_18766.log \
make acceptance
```

Only literal `PASS` is review-ready. On `FAIL`, fix, recommit, rerun affected
checks, and rerun the gate. On `BLOCKED`, report the blocker without substituting
curl, fixtures, mocks, screenshots, or unit tests.

- [ ] **Step 7: Redeploy the exact accepted SHA**

Record the accepted SHA, restart the preview from that unchanged worktree, and
wait for HTTP 200:

```bash
ACCEPTED_SHA="$(git rev-parse HEAD)"
screen -S open_trader_trend_preview -X quit
truncate -s 0 /tmp/open_trader_trend_preview_18766.log
screen -dmS open_trader_trend_preview zsh -lc \
  'cd /Users/ray/projects/open_trader/.worktrees/trend-holding-row-states && exec env PYTHONPATH=src .venv/bin/python -u -m open_trader dashboard --host 127.0.0.1 --port 18766 --portfolio /Users/ray/Library/Caches/open_trader/trend-real-holdings-preview-20260730/data/latest/portfolio.csv --data-dir /Users/ray/Library/Caches/open_trader/trend-real-holdings-preview-20260730/data --reports-dir /Users/ray/Library/Caches/open_trader/trend-real-holdings-preview-20260730/reports --config /Users/ray/projects/open_trader/config/daily_premarket.env --prediction-config /Users/ray/projects/open_trader/config/prediction_arbitrage.json >> /tmp/open_trader_trend_preview_18766.log 2>&1'
curl --fail --retry 20 --retry-delay 1 http://127.0.0.1:18766/
test "$(git rev-parse HEAD)" = "$ACCEPTED_SHA"
```

- [ ] **Step 8: Verify fresh runtime identity**

Run:

```bash
PREVIEW_PID="$(lsof -tiTCP:18766 -sTCP:LISTEN)"
ps -p "$PREVIEW_PID" -o pid=,lstart=,command=
lsof -a -p "$PREVIEW_PID" -d cwd -Fn
git rev-parse HEAD
tail -80 /tmp/open_trader_trend_preview_18766.log
curl --fail http://127.0.0.1:18766/api/dashboard >/dev/null
```

Verify the PID is fresh, cwd is the feature worktree, SHA equals
`ACCEPTED_SHA`, the log is fresh with no traceback, and both review endpoints
return HTTP 200. Do not merge or push until the user approves the deployed
feature branch.

## Execution Handoff

Plan complete. Execution may proceed either:

1. **Subagent-Driven** — a fresh implementation agent per task with review
   between tasks.
2. **Inline Execution** — execute this plan in the current session with
   checkpoints.

Given the small, shared three-file implementation, inline execution is the
shorter path.
