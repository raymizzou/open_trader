# Trend Report Real and Simulated Holding Tabs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single simulated-holding view inside `盘中持续 · 已有持仓` with default `真实持仓` and switchable `模拟盘持仓` tabs for CN, HK, and US, while keeping the existing table contract unchanged and hiding unavailable option-anomaly buttons.

**Architecture:** Freeze a best-effort, read-only real-account snapshot and its separately evaluated holding judgments into each newly generated report. Reuse the existing holding decision rules through one shared evaluator, but keep real protection state, report fields, Dashboard projection, and UI state separate from simulated strategy state and every execution path. The Dashboard renders the two frozen collections through the same ten-column table renderer; historical reports never join to current account data.

**Tech Stack:** Python 3.12, stdlib `dataclasses`/`Decimal`/CSV/JSON, pytest, existing Trend Animals and Futu clients, vanilla JavaScript/CSS, existing Playwright-backed Dashboard acceptance, macOS launchd.

## Global Constraints

- Continue in the isolated worktree `/Users/ray/projects/open_trader/.worktrees/trend-report-holding-source-tabs` on branch `feat/trend-report-holding-source-tabs`, which was created from local `main`.
- Do not modify the unrelated dirty files in `/Users/ray/projects/open_trader`.
- Do not add dependencies, a database, an API endpoint, a new background job, a new option-data request, or a real-account order path.
- Real holdings are read-only evidence. Only simulated holdings may affect `formal_actions`, report counts, Kelly, risk, drawdown, Feishu, execution batches, orders, reconciliation, or strategy state.
- Keep the existing `盘中持续 · 已有持仓` section style, table, all ten columns, column order, formatting, desktop layout, and mobile cards unchanged.
- The tab labels are exactly `真实持仓` and `模拟盘持仓`; do not add `我的`, quantity, source, badge, or another column.
- Default every newly rendered current or historical report to `真实持仓`. Do not persist the inner-tab choice or add it to the URL.
- A report uses only its frozen real snapshot and decisions. Never combine a historical report with the Dashboard's latest-account overlay.
- Updating/importing a statement does not rewrite an existing report. The next generated report consumes the new statement and freezes new decisions.
- Include every positive stock/ETF position for the market. Exclude cash, FX, funds, money-market funds, options, zero/negative quantities, and unsupported assets.
- Keep unavailable, available-empty, and legacy-report states distinct.
- A valid snapshot that omits a previously held symbol closes that real-position lifecycle. An unavailable snapshot must preserve the prior real protection state.
- Missing per-symbol signal or K-line data produces `MANUAL_REVIEW` and must not invent a new protection line.
- Keep the clickable option-anomaly button and native dialog unchanged when `available === true`. Render no button at all when data is missing or unavailable.
- Run focused tests during development. Run `make acceptance` only after source, tests, changelog, and live three-market artifacts are final.
- Before merging, commit a dated operator-facing `CHANGELOG.md` entry.
- Only `make acceptance` returning `PASS` is review-ready. After `PASS`, deploy the exact accepted SHA and verify the Dashboard and all three controllers from that SHA.

## Frozen Contracts

New reports may add these optional `strategy_judgments` fields:

```json
{
  "real_holding_decisions": [],
  "real_holding_decisions_status": "available",
  "real_holding_decisions_source": {
    "broker": "phillips",
    "broker_label": "辉立",
    "snapshot_period": "2026-07-29",
    "source_kind": "statement",
    "freshness_text": "非实时",
    "read_only_text": "只读，不自动下单"
  }
}
```

An unavailable snapshot uses:

```json
{
  "real_holding_decisions_status": "unavailable",
  "real_holding_decisions_reason": "未找到可用的辉立持仓结单",
  "real_holding_decisions_source": {
    "broker": "phillips",
    "broker_label": "辉立",
    "snapshot_period": "",
    "source_kind": "statement",
    "freshness_text": "非实时",
    "read_only_text": "只读，不自动下单"
  }
}
```

The Dashboard projection adds:

```json
{
  "real_position_actions": [],
  "real_position_status": "available",
  "real_position_reason": "",
  "real_position_source": {}
}
```

Legacy reports omit all four frozen fields and project:

```json
{
  "real_position_actions": [],
  "real_position_status": "legacy",
  "real_position_reason": "当前报告未包含真实持仓判断",
  "real_position_source": {}
}
```

The existing `hold_actions`, `counts`, `formal_actions`, `account`, `protection_state`, and Feishu payload meanings do not change.

---

### Task 1: Centralize the latest broker-detail snapshot boundary

**Files:**
- Create: `src/open_trader/broker_details.py`
- Modify: `src/open_trader/dashboard.py` near `_latest_broker_details` and `_latest_statement_period`
- Modify: `src/open_trader/market_trend.py` near `_latest_broker_rows`
- Create: `tests/test_broker_details.py`
- Test: `tests/test_dashboard.py`
- Test: `tests/test_market_trend.py`

**Interfaces:**
- Produces `BrokerDetailSnapshot`.
- Produces `load_broker_detail_snapshot(data_dir: Path, broker: str) -> BrokerDetailSnapshot`.
- Preserves the existing Dashboard `_latest_broker_details(...)` return shape through a thin compatibility wrapper.

- [ ] **Step 1: Write failing snapshot-selection tests**

Create table-driven tests for `tiger`, `phillips`, and `eastmoney`:

```python
snapshot = load_broker_detail_snapshot(data_dir, "phillips")

assert snapshot.broker == "phillips"
assert snapshot.snapshot_period == "2026-07-29"
assert snapshot.source_kind == "statement"
assert [row["symbol"] for row in snapshot.positions] == ["3690", "9858"]
```

Cover these exact cases:

1. a monthly run directory containing a newer statement beats a lexically newer daily directory containing an older statement;
2. Tiger live rows ending in `-tiger-live` beat Tiger statement-only rows;
3. positions and cash from the selected broker snapshot stay paired;
4. zero-quantity rows remain available to callers for later filtering, rather than changing the shared detail-loader contract;
5. missing `data/runs` returns an unavailable snapshot with empty rows and a reason.

- [ ] **Step 2: Run the new tests and verify RED**

```bash
PYTHONPATH=.:src /Users/ray/projects/open_trader/.venv/bin/pytest -q \
  tests/test_broker_details.py
```

Expected: import failure because `broker_details.py` does not exist.

- [ ] **Step 3: Implement one small broker-detail loader**

Define:

```python
@dataclass(frozen=True)
class BrokerDetailSnapshot:
    broker: str
    positions: tuple[dict[str, str], ...]
    cash: tuple[dict[str, str], ...]
    snapshot_period: str
    source_kind: str
    available: bool
    reason: str


def load_broker_detail_snapshot(
    data_dir: Path,
    broker: str,
) -> BrokerDetailSnapshot:
    return BrokerDetailSnapshot(
        broker=broker,
        positions=selected_positions,
        cash=selected_cash,
        snapshot_period=selected_period,
        source_kind=selected_kind,
        available=bool(selected_positions or selected_cash),
        reason=unavailable_reason,
    )
```

Use the existing `data/runs/*/extracted_positions.csv` and
`extracted_cash.csv` files. For `phillips` and `eastmoney`, select the candidate
with the greatest date/month parsed from `statement_id`, using run-directory
name only as a deterministic tie-breaker. For Tiger, prefer the newest valid
live snapshot; fall back to the newest statement snapshot. Reject unsupported
broker names.

Do not infer holdings, asset classes, or trading decisions in this module.

- [ ] **Step 4: Replace duplicate readers with compatibility wrappers**

Make `dashboard._latest_broker_details(...)` call the shared loader for the
existing four Dashboard brokers and return the same two lists it returns now.
Make `market_trend._latest_broker_rows(...)` call the shared loader and return
the same tuple it returns now. Keep private wrappers so existing callers and
tests do not need a broad rename.

- [ ] **Step 5: Prove no current account display/report behavior regressed**

```bash
PYTHONPATH=.:src /Users/ray/projects/open_trader/.venv/bin/pytest -q \
  tests/test_broker_details.py \
  tests/test_dashboard.py -k 'latest_broker_details or source_status or actual_overlay' \
  tests/test_market_trend.py -k 'load_market_account or stale_statement'
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add \
  src/open_trader/broker_details.py \
  src/open_trader/dashboard.py \
  src/open_trader/market_trend.py \
  tests/test_broker_details.py \
  tests/test_dashboard.py \
  tests/test_market_trend.py
git commit -m "refactor: share latest broker detail snapshots"
```

---

### Task 2: Extract the shared holding evaluator and add a read-only real mode

**Files:**
- Modify: `src/open_trader/a_share_trend.py` near `HoldingDecision`, `TrendReport`, and `build_report`
- Test: `tests/test_a_share_trend.py` near the existing holding/protection tests

**Interfaces:**
- Adds `RealHoldingInput`.
- Adds internal `_evaluate_holding_positions(...) -> HoldingEvaluation`.
- Changes `build_report(..., real_holdings: RealHoldingInput | None = None)`.
- Adds optional internal `TrendReport` fields for real decisions, source/status, and real protection state.

- [ ] **Step 1: Add failing extraction-regression tests for simulated holdings**

Parameterize representative existing scenarios before extracting code:

- normal `HOLD`;
- `SELL_ALL` from danger/left-side;
- `SELL_PARTIAL` with HK lot size;
- stale K-line to `MANUAL_REVIEW`;
- triggered protection replay;
- existing active line remains non-decreasing.

Assert the exact `HoldingDecision` values and `report.protection_state` before
and after extraction. These tests lock the current simulated behavior and
prevent the refactor from changing strategy output.

- [ ] **Step 2: Run the holding slice and record GREEN before refactoring**

```bash
PYTHONPATH=.:src /Users/ray/projects/open_trader/.venv/bin/pytest -q \
  tests/test_a_share_trend.py -k \
  'holding_action or holding_kline or protection_line or triggered_protection'
```

Expected: the pre-existing behavior passes.

- [ ] **Step 3: Extract the current loop without changing simulated semantics**

Move only the holding loop from `build_report` into:

```python
@dataclass(frozen=True)
class HoldingEvaluation:
    decisions: tuple[HoldingDecision, ...]
    protection_state: dict[str, object]
    industry_counts: Counter[str]
    industry_values: dict[str, Decimal]


def _evaluate_holding_positions(
    *,
    positions: Sequence[AccountPosition],
    holding_snapshots: Mapping[str, HoldingSnapshot | None],
    bars_by_symbol: Mapping[str, Sequence[DailyKlineBar] | None],
    prior_state: Mapping[str, object] | None,
    watch_events: Sequence[Mapping[str, object]],
    as_of_date: str,
    market: str,
    lot_sizes: Mapping[str, int] | None,
    current_exit_discipline: bool,
    read_only_real: bool,
) -> HoldingEvaluation:
    return HoldingEvaluation(
        decisions=tuple(decisions),
        protection_state={"schema_version": 1, "positions": new_positions},
        industry_counts=industry_counts,
        industry_values=dict(industry_values),
    )
```

Call it with `read_only_real=False` for the existing simulated account. Keep
all current simulated initialization, trimming, warnings, and state fields
byte-for-byte equivalent at the payload boundary.

- [ ] **Step 4: Write failing real-mode protection tests**

Add tests asserting:

```python
real = _build_report(real_holdings=real_input)
decision = real.real_holdings[0]

assert decision.action == "HOLD"
assert decision.active_line == max(old_line, recalculated_line)
assert real.real_protection_state["positions"]["AAPL"]["active_line"] == str(
    decision.active_line
)
assert real.protection_state == simulated_state_before
```

Cover:

1. initial line uses valid average cost and current ATR;
2. a continuing line is `max(old_active_line, recalculated_line)`;
3. quantity/cost changes do not reset `position_started_for`;
4. available omission drops the old symbol from new real state;
5. reappearance after that omission creates a new lifecycle;
6. missing signal or missing/stale K-line yields `MANUAL_REVIEW`;
7. missing signal/K-line with no prior line leaves both line fields empty;
8. missing signal/K-line with a prior line preserves the prior line;
9. real `SELL_ALL`, `SELL_PARTIAL`, and `MANUAL_REVIEW` remain in the real collection;
10. real decisions never enter `formal_actions`.

- [ ] **Step 5: Implement the read-only real branch**

Define:

```python
@dataclass(frozen=True)
class RealHoldingInput:
    status: str
    reason: str
    source: dict[str, str]
    positions: tuple[AccountPosition, ...]
    holding_snapshots: Mapping[str, HoldingSnapshot | None]
    bars_by_symbol: Mapping[str, Sequence[DailyKlineBar] | None]
    prior_state: Mapping[str, object] | None
```

Rules:

- `status` is exactly `available` or `unavailable`;
- unavailable input produces no decisions and no replacement real state;
- available empty input produces an empty decisions tuple and an empty
  `{"schema_version": 1, "positions": {}}` state;
- real mode recalculates a valid candidate line, then clamps it upward to the
  prior active line;
- real mode initializes/recalculates only when both the same-date signal
  snapshot and valid same-date K-line metrics exist;
- the real path never contributes industry counts/value, cash, sell proceeds,
  position slots, risk, Kelly, drawdown, candidate exclusion, or buys.

- [ ] **Step 6: Freeze only the optional real report fields**

Extend `_report_payload(...)` so:

- `real_holdings is None` omits every new field for backward compatibility;
- available input writes decisions, status, and source;
- unavailable input writes status, reason, and source but no fabricated empty
  collection;
- `formal_actions` continues to derive only from `report.holdings` and
  `report.buy_actions`;
- `signal_snapshots.real_holdings` freezes real per-symbol facts separately
  from `signal_snapshots.holdings`.

Do not add real decisions to `render_markdown`,
`render_trend_feishu_text`, report counts, or the simulated top-level
`protection_state`.

- [ ] **Step 7: Run the full report-core slice**

```bash
PYTHONPATH=.:src /Users/ray/projects/open_trader/.venv/bin/pytest -q \
  tests/test_a_share_trend.py -k \
  'holding or protection or report_payload or formal_actions or feishu'
```

Expected: all selected tests pass and the existing Feishu snapshots remain
unchanged.

- [ ] **Step 8: Commit**

```bash
git add src/open_trader/a_share_trend.py tests/test_a_share_trend.py
git commit -m "feat: evaluate frozen real holdings read only"
```

---

### Task 3: Feed frozen real snapshots through CN, HK, and US report generation

**Files:**
- Modify: `src/open_trader/a_share_trend.py` near `_attempt_report`, receipt recovery, and state paths
- Modify: `src/open_trader/market_trend.py` near `MarketTrendPaths`, `_attempt_market_report`, and receipt recovery
- Test: `tests/test_a_share_trend.py`
- Test: `tests/test_market_trend.py`

**Interfaces:**
- Consumes `load_broker_detail_snapshot(...)`.
- Adds `real_protection_state.json` beside each market's existing simulated `protection_state.json`.
- Extends delivery receipts with an optional `real_protection_state`.

- [ ] **Step 1: Write failing loader/filter tests for all three markets**

For CN/Eastmoney, HK/Phillips, and US/Tiger, create extracted detail rows with:

- one positive stock;
- one positive ETF;
- one option;
- one money-market fund;
- one zero-quantity stock;
- one row from another market.

Assert each runner freezes exactly the stock and ETF for its market, with
normalized Futu-compatible symbols, quantity, name, and optional average cost.
Assert the source object contains the exact configured broker label, period,
kind, freshness, and read-only text.

- [ ] **Step 2: Run the new runner tests and verify RED**

```bash
PYTHONPATH=.:src /Users/ray/projects/open_trader/.venv/bin/pytest -q \
  tests/test_a_share_trend.py -k 'real_holding_snapshot' \
  tests/test_market_trend.py -k 'real_holding_snapshot'
```

Expected: failures because runners do not yet load/freeze real positions.

- [ ] **Step 3: Add the fail-closed real snapshot adapter**

Build `AccountPosition` rows only from the selected broker snapshot:

```python
RealHoldingInput(
    status="available",
    reason="",
    source={
        "broker": broker,
        "broker_label": broker_label,
        "snapshot_period": detail.snapshot_period,
        "source_kind": detail.source_kind,
        "freshness_text": "实时" if detail.source_kind == "live_account" else "非实时",
        "read_only_text": "只读，不自动下单",
    },
    positions=real_positions,
    holding_snapshots=real_holding_snapshots,
    bars_by_symbol=real_bars_by_symbol,
    prior_state=real_prior_state,
)
```

Use the explicit `asset_class` when valid; otherwise use the existing
`detect_asset_class(symbol, name)` fallback. If a retained row has an invalid
symbol, quantity, or market, mark the real snapshot unavailable rather than
silently presenting a partial account. This failure must not abort simulated
report generation.

- [ ] **Step 4: Resolve real-only signals and K-lines independently**

For each market:

1. load the real snapshot before assembling holding requests;
2. reuse already fetched candidate/simulated `tm_id`, snapshot, and K-line
   results for overlapping symbols;
3. resolve additional real-only symbols separately;
4. fetch additional real-only Trend Animals snapshots in a separate
   best-effort call, so a failure cannot abort the simulated report;
5. fetch real-only Futu K-lines best-effort, treating even a systemic real-only
   error as missing read-only evidence rather than a simulated-report failure;
6. leave a row present as `MANUAL_REVIEW` when either read-only source is
   unavailable.

Count only successful real-only paid snapshot rows in the estimate, while the
existing balance delta remains the source of actual API cost. Do not add a new
request for overlapping symbols.

- [ ] **Step 5: Add separate durable real protection state**

Use:

```text
data/trend_a_share/real_protection_state.json
data/trend_hk_phillips/real_protection_state.json
data/trend_us_tiger/real_protection_state.json
```

Add `real_state` to `MarketTrendPaths`. Load this state independently from the
simulated state. Write it only when the real snapshot status is `available`.
When unavailable, preserve the prior file byte-for-byte.

Add optional `real_protection_state` to delivery receipts. Receipt recovery
must replay it when present and available, while legacy receipts without it
continue to work. This keeps report/state persistence crash-recoverable without
putting real state into the simulated `protection_state`.

- [ ] **Step 6: Prove statement timing and lifecycle semantics**

Add integration tests:

1. generate a report from statement period `2026-07-29`;
2. import/write a `2026-07-30` detail snapshot;
3. assert the old report bytes/hash and source period remain unchanged;
4. generate the next report and assert it freezes `2026-07-30`;
5. feed an unavailable snapshot and assert simulated report generation succeeds
   and the real state file is unchanged;
6. feed a valid empty snapshot and assert the next state is empty;
7. reintroduce the symbol and assert a new protection lifecycle initializes.

- [ ] **Step 7: Prove strategy/notification isolation**

For each market, compare the same simulated account with and without real
positions and assert these are identical:

```python
assert after["strategy_judgments"]["formal_actions"] == before[
    "strategy_judgments"
]["formal_actions"]
assert after["account"] == before["account"]
assert after["risk_summary"] == before["risk_summary"]
assert render_trend_feishu_text(
    after, broker_label=broker_label, market_label=market
)[1] == render_trend_feishu_text(
    before, broker_label=broker_label, market_label=market
)[1]
```

Also assert no real symbol appears in execution inputs unless it independently
already exists in the simulated formal actions.

- [ ] **Step 8: Run three-market generation/recovery tests**

```bash
PYTHONPATH=.:src /Users/ray/projects/open_trader/.venv/bin/pytest -q \
  tests/test_a_share_trend.py -k \
  'report_runner or real_holding or receipt or feishu' \
  tests/test_market_trend.py -k \
  'market_report or real_holding or recovery or receipt or notification'
```

Expected: all selected tests pass.

- [ ] **Step 9: Commit**

```bash
git add \
  src/open_trader/a_share_trend.py \
  src/open_trader/market_trend.py \
  tests/test_a_share_trend.py \
  tests/test_market_trend.py
git commit -m "feat: freeze real holdings in three market reports"
```

---

### Task 4: Project frozen real decisions without touching current-account overlays

**Files:**
- Modify: `src/open_trader/dashboard.py` near `_valid_trend_collections`, `_project_trend_actions`, and `_project_broker_trend_report`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Adds `_project_trend_real_actions(...)`.
- Produces `real_position_actions`, `real_position_status`,
  `real_position_reason`, and `real_position_source`.
- Leaves `_project_trend_actual_overlay(...)` unchanged and separate.

- [ ] **Step 1: Write failing validation/projection tests**

Add cases for:

1. available with rows;
2. available with zero rows;
3. unavailable with a reason;
4. legacy fields absent;
5. malformed status, collection, source, or reason rejects the artifact;
6. a historical report projects its own frozen rows even when
   `broker_positions` contains a newer/different account;
7. the input payload remains unmodified.

Assert the exact status/reason contracts from this plan.

- [ ] **Step 2: Add urgency-first ordering tests**

Supply rows in reverse order and assert:

```python
assert [item["action"] for item in projected] == [
    "SELL_ALL",
    "SELL_PARTIAL",
    "MANUAL_REVIEW",
    "HOLD",
]
```

Within each action group, assert `_project_trend_sorted_holdings(...)` preserves
the current frozen industry-first/individual fallback order. Assert existing
`hold_actions` ordering remains unchanged.

- [ ] **Step 3: Run the projection tests and verify RED**

```bash
PYTHONPATH=.:src /Users/ray/projects/open_trader/.venv/bin/pytest -q \
  tests/test_dashboard.py -k \
  'real_position or real_holding or legacy_real or historical_real'
```

Expected: missing projection fields/helpers.

- [ ] **Step 4: Validate optional fields without invalidating legacy reports**

Extend `_valid_trend_collections(...)` with these exact rules:

- no new keys: valid legacy report;
- `available`: requires a list of dictionaries and a valid source object;
- `unavailable`: requires a nonempty reason and valid source object;
- any partial or contradictory combination: invalid artifact.

Do not require real fields for old reports and do not rewrite old JSON.

- [ ] **Step 5: Project frozen rows and source metadata**

Enrich real rows only from `signal_snapshots.real_holdings`. Group by urgency,
sort each group with the existing holding sorter, then concatenate the groups.
Do not read from `broker_positions`, `_latest_broker_details`, or
`actual_overlay`.

Attach existing same-report-date option anomaly data to US/HK
`real_position_actions` as well as buy/simulated-hold rows. Make no new
option-data request.

- [ ] **Step 6: Prove simulated counts and execution projection are unchanged**

Assert:

```python
assert report["counts"] == {
    "sell": len(report["sell_actions"]),
    "buy": len(report["buy_actions"]),
    "hold": len(report["hold_actions"]),
    "review": len(report["review_actions"]),
}
assert "real_position_actions" not in report["actual_overlay"]["items"]
```

The second assertion may inspect symbols/items rather than literal list
membership, but must prove the current-account overlay and execution projection
did not absorb the new read-only rows.

- [ ] **Step 7: Run Dashboard backend tests**

```bash
PYTHONPATH=.:src /Users/ray/projects/open_trader/.venv/bin/pytest -q \
  tests/test_dashboard.py -k \
  'trend_report or trend_actions or real_position or actual_overlay or option_anomaly'
```

Expected: all selected tests pass.

- [ ] **Step 8: Commit**

```bash
git add src/open_trader/dashboard.py tests/test_dashboard.py
git commit -m "feat: project frozen real trend holdings"
```

---

### Task 5: Render the approved two-tab holding section with the unchanged table

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js` near `renderTrendSellOrHoldStage` and report event handlers
- Test: `tests/test_dashboard_web.py`

**Interfaces:**
- Adds `renderTrendHoldingStage(report)`.
- Adds `handleTrendHoldingTab(event)` and
  `handleTrendHoldingTabKeydown(event)`.
- Reuses `.account-view-tabs`, `.account-view-tab`, `.cn-trend-table`, and
  `.cn-trend-card`; no new visual system.

- [ ] **Step 1: Write the failing exact-markup test**

Render CN, HK, and US reports and assert:

```javascript
const tabs = html.match(/data-trend-holding-view="[^"]+"/g) || [];
if (tabs.length !== 2) throw new Error(html);
if (!html.includes(">真实持仓</button>")) throw new Error(html);
if (!html.includes(">模拟盘持仓</button>")) throw new Error(html);
if (!html.includes('data-trend-holding-view="real" aria-selected="true"')) {
  throw new Error(html);
}
```

Assert `我的真实持仓` and `我的模拟盘持仓` never appear.

- [ ] **Step 2: Lock the exact table contract for both panels**

For each market and both panels, assert one table has exactly:

```python
[
    "标的",
    "动作",
    "执行参考价",
    "温度变化",
    "节气",
    "强度",
    "行业",
    "当前判断",
    "活动保护线",
    "持仓提示",
]
```

Assert the current `.cn-trend-card` cell labels/order are identical in real and
simulated panels. Do not snapshot a redesigned table.

- [ ] **Step 3: Refactor only the holding row/table renderer**

Extract the current holding headings and row mapping from
`renderTrendSellOrHoldStage(...)` into a helper shared by both panels.

Map real action cells exactly:

```javascript
const labels = {
  SELL_ALL: "全部卖出",
  SELL_PARTIAL: "止盈减仓 30%",
  MANUAL_REVIEW: "人工复核",
  HOLD: "继续持有",
};
```

Keep simulated `HOLD` output exactly `继续持有`. Use the existing
`trendReasonLabel`, `trendHints`, number formatting, identity rendering,
escaping, and responsive table classes.

- [ ] **Step 4: Add the minimal tab wrapper**

Render:

1. the unchanged `<h2>盘中持续 · 已有持仓</h2>`;
2. a two-button tablist directly below it;
3. the real source line directly above the real table;
4. one real and one simulated tabpanel using the shared table.

Use the existing tab classes so selected underline, font, spacing, 44px target,
and mobile overflow behavior are inherited. Use native `hidden` on the inactive
panel; add no persistence or URL state.

Render the real source line as:

```text
辉立 · 结单 2026-07-29 · 非实时 · 只读，不自动下单
```

Use `账户` instead of `结单` for `source_kind == "live_account"`.

Render exact states:

- legacy: `当前报告未包含真实持仓判断`;
- unavailable: `真实持仓数据不可用：{reason}`;
- available empty: `当前无真实持仓`;
- simulated empty: `当前无模拟盘持仓`.

- [ ] **Step 5: Add local accessible tab behavior**

`handleTrendHoldingTab(...)` operates only on the nearest holding section:

- update `aria-selected` and `tabindex`;
- update both tabpanels' `hidden` state;
- preserve all report/account outer-tab state.

Support `ArrowLeft`, `ArrowRight`, `Home`, and `End`, matching the existing
account tabs. Register click and keydown delegation on both
`#account-holdings` and `#trend-report-workspace`. Every newly rendered current
or historical report starts on real.

- [ ] **Step 6: Run renderer, keyboard, and mobile tests**

```bash
PYTHONPATH=.:src /Users/ray/projects/open_trader/.venv/bin/pytest -q \
  tests/test_dashboard_web.py -k \
  'trend_report or holding_tab or cross_market or mobile_layout'
```

Expected: all selected tests pass, the existing CSS contract remains
sufficient, and no source/CSS change is needed.

- [ ] **Step 7: Commit**

```bash
git add \
  src/open_trader/dashboard_static/dashboard.js \
  tests/test_dashboard_web.py
git commit -m "feat: add real and simulated trend holding tabs"
```

---

### Task 6: Remove unavailable option-anomaly controls

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js` near `renderTrendOptionIdentityCell`
- Modify: `src/open_trader/dashboard_acceptance.py` near `_check_trend_option_buttons`
- Test: `tests/test_dashboard_web.py`
- Test: `tests/test_dashboard_acceptance.py`

**Interfaces:**
- Preserves available `.trend-option-button` and native dialog behavior.
- Removes the disabled-button branch entirely.

- [ ] **Step 1: Change the renderer test to the approved behavior**

Keep the available case assertions:

```javascript
if ((html.match(/data-option-anomaly-open/g) || []).length !== 2) {
  throw new Error(html);
}
if ((html.match(/<dialog class="trend-option-dialog"/g) || []).length !== 2) {
  throw new Error(html);
}
```

Replace the disabled assertion with:

```javascript
if (html.includes('disabled title="富途未返回该标的期权异动"')) {
  throw new Error(html);
}
if ((html.match(/>期权异动<\/button>/g) || []).length !== 2) {
  throw new Error(html);
}
```

Include available/missing rows in buy, simulated holdings, and real holdings.
A-share still renders no option control.

- [ ] **Step 2: Run the option renderer test and verify RED**

```bash
PYTHONPATH=.:src /Users/ray/projects/open_trader/.venv/bin/pytest -q \
  tests/test_dashboard_web.py::test_dashboard_renders_option_anomaly_button_and_native_dialog
```

Expected: failure because missing anomalies still render disabled buttons.

- [ ] **Step 3: Delete only the unavailable button branch**

Make `renderTrendOptionIdentityCell(item)` return the ordinary identity cell
when `anomaly.available !== true`. When available, keep the current button,
ARIA label, dialog markup, escaping, open/close behavior, and styles unchanged.

- [ ] **Step 4: Update acceptance to count only available controls**

In `_check_trend_option_buttons(...)`, build the expected action list from
`buy_actions`, `hold_actions`, and `real_position_actions`, then retain only
rows whose `option_anomaly.available is True`.

Assert:

- visible button count equals available-row count;
- no `.trend-option-button:disabled` exists;
- the first available button opens the correct native dialog and closes;
- heading count remains unchanged;
- zero available rows means zero option buttons and is valid.

Replace fake-page tests for disabled state with tests that reject an extra
button for a missing anomaly.

- [ ] **Step 5: Run focused option and acceptance tests**

```bash
PYTHONPATH=.:src /Users/ray/projects/open_trader/.venv/bin/pytest -q \
  tests/test_dashboard_web.py -k 'option_anomaly' \
  tests/test_dashboard_acceptance.py -k 'option_anomaly'
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add \
  src/open_trader/dashboard_static/dashboard.js \
  src/open_trader/dashboard_acceptance.py \
  tests/test_dashboard_web.py \
  tests/test_dashboard_acceptance.py
git commit -m "fix: hide unavailable option anomaly buttons"
```

---

### Task 7: Extend Dashboard acceptance for both frozen holding panels

**Files:**
- Modify: `src/open_trader/dashboard_acceptance.py` near trend-stage checks
- Modify: `tests/test_dashboard_acceptance.py`
- Modify: `CHANGELOG.md` in the `2026-07-30` section

**Interfaces:**
- Acceptance proves both tabs use the existing ten-column report UI across CN,
  HK, and US.
- Changelog records the operator-visible behavior and verification.

- [ ] **Step 1: Add failing acceptance assertions**

For each available Tiger/Phillips/Eastmoney report:

- assert the two inner tabs exist in exact order;
- assert `真实持仓` is selected initially;
- assert the selected panel's ten headings exactly match the approved list;
- compare visible real rows to `real_position_actions`;
- switch to `模拟盘持仓`;
- assert the same ten headings and compare rows to `hold_actions`;
- switch back to real and assert keyboard focus/ARIA state;
- distinguish legacy, unavailable, and available-empty text;
- on mobile, assert both panels retain `.cn-trend-card`, no horizontal page
  overflow, and all visible tab/option controls meet the existing target check.

Keep all existing sell, buy, review, discipline, risk, and audit acceptance
checks unchanged.

- [ ] **Step 2: Update fake acceptance payloads**

Add representative frozen real fields to Tiger, Phillips, and Eastmoney:

- one available multi-action real list;
- one available-empty report;
- one unavailable report;
- one legacy historical report.

Keep existing simulated counts/actions unchanged so tests prove the new rows do
not alter strategy metrics.

- [ ] **Step 3: Run acceptance-unit tests**

```bash
PYTHONPATH=.:src /Users/ray/projects/open_trader/.venv/bin/pytest -q \
  tests/test_dashboard_acceptance.py -k \
  'trend_report or account_holdings or holding_tab or option_anomaly'
```

Expected: all selected tests pass.

- [ ] **Step 4: Add the dated operator-facing changelog entry**

Under `## 2026-07-30`, record:

- CN/HK/US `盘中持续 · 已有持仓` now defaults to frozen `真实持仓` and switches to
  frozen `模拟盘持仓`;
- real evidence is read-only and simulated strategy/execution remains the only
  action source;
- missing option-anomaly data no longer renders a control;
- focused tests and three-market live validation completed.

Do not claim acceptance PASS in the changelog until the gate actually passes;
use wording that can be finalized before the acceptance commit without a
follow-up source commit.

- [ ] **Step 5: Run all affected suites and static checks**

```bash
PYTHONPATH=.:src /Users/ray/projects/open_trader/.venv/bin/pytest -q \
  tests/test_broker_details.py \
  tests/test_a_share_trend.py \
  tests/test_market_trend.py \
  tests/test_dashboard.py \
  tests/test_dashboard_web.py \
  tests/test_dashboard_acceptance.py
git diff --check
```

Expected: exit 0, zero test failures, and no whitespace errors.

- [ ] **Step 6: Commit acceptance coverage and changelog before merge**

```bash
git add \
  src/open_trader/dashboard_acceptance.py \
  tests/test_dashboard_acceptance.py \
  CHANGELOG.md
git commit -m "test: accept real and simulated trend holdings"
```

---

### Task 8: Validate real CN/HK/US artifacts, run the final gate, merge, and deploy

**Files/runtime outputs:**
- Reports: `reports/trend_a_share`, `reports/trend_hk_phillips`, `reports/trend_us_tiger`
- Real state: `data/trend_a_share/real_protection_state.json`,
  `data/trend_hk_phillips/real_protection_state.json`,
  `data/trend_us_tiger/real_protection_state.json`
- Controller evidence: `data/trend_controller/{CN,HK,US}/status.json`
- Dashboard evidence: runtime metadata and `logs/dashboard/launchd.*.log`

**Interfaces:**
- Consumes the final committed feature SHA.
- Produces immutable three-market report revisions, a final acceptance result,
  an exact-SHA `main`, and a live review URL.

- [ ] **Step 1: Reconcile latest local `main` before final validation**

```bash
git status --short
git fetch origin
git merge main
git diff --check
```

Resolve only in-scope conflicts. If `main` advances after this point, merge it
into the feature branch and repeat all steps from the focused suite onward.

- [ ] **Step 2: Capture pre-run immutability and execution evidence**

Record:

- feature SHA;
- selected CN/HK/US report artifact names and SHA-256 hashes;
- execution-batch/action-ledger file counts and hashes;
- existing real/simulated protection-state hashes;
- `trend-market status` output for all three markets.

Use:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m open_trader \
  trend-market status --market CN \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m open_trader \
  trend-market status --market HK \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m open_trader \
  trend-market status --market US \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env
```

- [ ] **Step 3: Generate one explicit immutable revision per market**

Run the existing revision/no-submit workflow:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m open_trader \
  trend-market run --market CN --revision \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m open_trader \
  trend-market run --market HK --revision \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m open_trader \
  trend-market run --market US --revision \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env
```

Confirm each command returns a generated/revised artifact. Confirm no broker
order submission and no new action-ledger submission occurred. Existing report
files must retain their pre-run hashes; each revision is a new immutable file.

- [ ] **Step 4: Verify every market's frozen contract directly**

For the new CN/HK/US JSON artifacts, assert:

- `metadata.market` and configured real broker match;
- `real_holding_decisions_status` is `available` or has a visible unavailable
  reason;
- available rows equal all eligible positive real stock/ETF rows in the
  selected source snapshot;
- excluded cash/FX/fund/money-market/option rows are absent;
- source period/kind/freshness/read-only fields are correct;
- real rows never appear in `formal_actions` unless the symbol independently
  belongs to the simulated actions;
- counts equal simulated projected actions only;
- Feishu text contains no new real-holding section;
- real protection state is separate from simulated state;
- a historical pre-run artifact still projects its original legacy/frozen real
  state, never the new source snapshot;
- available option anomalies have buttons in the projected Dashboard data;
  missing anomalies project no visible control.

- [ ] **Step 5: Run the final Dashboard acceptance gate**

With all source, tests, changelog, and runtime report changes final:

```bash
make acceptance
```

Only literal `PASS` continues. On `FAIL`, diagnose and fix, recommit, regenerate
only the affected immutable revision, and rerun `make acceptance`. On
`BLOCKED`, stop and report the blocker; do not substitute curl, fixtures,
screenshots, or unit tests.

- [ ] **Step 6: Fast-forward `main` to the exact accepted SHA**

Record:

```bash
ACCEPTED_SHA="$(git rev-parse HEAD)"
```

From the main checkout, require a clean in-scope merge and preserve unrelated
user files:

```bash
git merge --ff-only feat/trend-report-holding-source-tabs
test "$(git rev-parse HEAD)" = "$ACCEPTED_SHA"
git push origin main
test "$(git rev-parse origin/main)" = "$ACCEPTED_SHA"
```

If fast-forward is impossible because `main` moved, do not create an unaccepted
merge commit. Merge the new `main` into the feature branch, rerun focused tests,
all three artifact checks, and `make acceptance`, then fast-forward.

- [ ] **Step 7: Redeploy the exact accepted SHA**

From `/Users/ray/projects/open_trader` at `ACCEPTED_SHA`:

```bash
scripts/install_dashboard_launchd.sh
scripts/install_daily_premarket_launchd.sh --market CN
scripts/install_daily_premarket_launchd.sh --market HK
scripts/install_daily_premarket_launchd.sh --market US
```

Do not make source/data changes after acceptance and before this restart.

- [ ] **Step 8: Verify fresh live process identity and behavior**

Verify:

- Dashboard launchd PID, cwd `/Users/ray/projects/open_trader`, and
  `dashboard_runtime` Git SHA equal `ACCEPTED_SHA`;
- Dashboard startup log has a fresh timestamp and no new traceback;
- `curl --fail http://127.0.0.1:8766/` and
  `curl --fail http://127.0.0.1:8766/api/dashboard` return HTTP 200;
- CN/HK/US launchd PIDs are fresh and each process cwd/SHA equals
  `ACCEPTED_SHA`;
- each `data/trend_controller/<MARKET>/status.json` has a fresh heartbeat,
  expected process version, and no new blocker;
- the live API exposes `real_position_actions` and the three real statuses;
- desktop and mobile browser checks show default `真实持仓`, working
  `模拟盘持仓` switching, all ten unchanged columns/cards, and no unavailable
  option buttons.

The review URL is:

```text
http://127.0.0.1:8766/
```

- [ ] **Step 9: Report evidence, not just completion**

Return the accepted/pushed SHA, exact focused-test totals, literal acceptance
result, three market artifact/status summaries, Dashboard/controller PIDs and
SHA/cwd proof, fresh-log timestamps, and the review URL.
