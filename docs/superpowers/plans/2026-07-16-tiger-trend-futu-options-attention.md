# Tiger Trend and Futu Options Attention Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move US trend trading from Futu to Tiger, expose Futu as a read-only US/HK options-attention aggregate, and render deterministic Trend Animals field transitions in one fixed-order list.

**Architecture:** Reuse the existing `market_trend.py` US/HK report pipeline and its frozen JSON as the single source of truth. Change only the US broker/account adapter, enrich the shared trend snapshot schema, calculate option attention while freezing each source report, and let Dashboard aggregate Tiger US plus Phillips HK without a third trend engine or notification job.

**Tech Stack:** Python 3 dataclasses and JSON, existing Trend Animals/Futu/Tiger clients, vanilla JavaScript/CSS, pytest, launchd, screen.

## Global Constraints

- Tiger owns the US trend report; Phillips owns HK; Eastmoney owns CN; Futu owns only the cross-market options-attention entry.
- Futu attention consumes Tiger US and Phillips HK reports only. It never consumes A-share data or the Futu watchlist.
- Request exactly the unified Trend Animals fields listed in Task 1 for US, HK, and CN; the catalog cost is `0.071` per symbol.
- Missing expansion values serialize as `null` and render as `未提供`; missing `isTrendRightSide`, `stopwinFlagByDangerSignal`, or a valid account snapshot remains a review/failure boundary.
- Option attention contains underlying symbols and raw field transitions only. It never recommends contracts, expiries, strikes, or orders.
- Protection-line events remain trading/watcher facts and never create option-attention rows.
- Tiger new positions use `4%` of full Tiger NAV in HKD, USD/HKD `7.85`, all-currency net available cash as the cap, no margin, and at most 10 positions.
- A stale but valid Tiger account snapshot produces no new buys and routes loaded holdings to manual review. No valid Tiger snapshot fails the US report.
- A stale trend report remains visible with its data date and creates no new transition. A later valid report replaces it in the Dashboard view.
- Do not add a generic multi-broker strategy framework, a new dependency, a third Futu report job, or a third Futu Feishu message.
- Do not run `make acceptance` before Task 7. Only its final `PASS` makes the Dashboard review-ready.

---

### Task 1: Unify the Trend Animals snapshot contract

**Files:**
- Modify: `src/open_trader/a_share_trend.py:39-71,95-180,431-489,943-1031,2157-2211,2280-2410`
- Modify: `src/open_trader/market_trend.py:500-580`
- Test: `tests/test_a_share_trend.py`
- Test: `tests/test_market_trend.py`

**Interfaces:**
- Produces: `UNIFIED_TREND_FIELDS: tuple[str, ...]` with one exact field order for all markets.
- Produces: normalized snake-case values in `signal_snapshots.candidates` and `signal_snapshots.holdings`.
- Consumes: existing `TrendAnimalsClient.get_snapshots()` and billing validation.

- [ ] **Step 1: Add failing field-contract tests**

Add this exact assertion to `tests/test_a_share_trend.py` and use the same constant in `tests/test_market_trend.py` fake billing/snapshot expectations:

```python
def test_unified_trend_fields_match_the_paid_catalog_selection() -> None:
    from open_trader.a_share_trend import UNIFIED_TREND_FIELDS

    assert UNIFIED_TREND_FIELDS == (
        "tmId", "tickerName", "tickerSymbol", "asset", "asOfDate",
        "tradableFlag", "industryTmId", "industryName", "priceIndex",
        "marketCap", "amount1d", "isTrendRightSide",
        "trendTemperatureCurr", "trendTemperaturePrev",
        "daysSinceTrendEntry", "gainSinceTrendEntry",
        "trendPhasePrev", "trendPhaseCurr", "trendStrengthLocalCurr",
        "trendStrengthLocalChange", "trendStrengthGlobalCurr",
        "trendStrengthLocalPrevWeek", "trendStrengthLocalPrevMonth",
        "stopwinFlagByDangerSignal",
        "stopwinFlagByBoilingTemperature",
        "stopwinFlagByPopChampagne", "tickerLabels",
    )
```

Add one candidate fixture containing every field and assert the serialized candidate signal contains these exact normalized keys and values:

```python
assert signal | {
    "gain_since_entry": "0.048",
    "phase_prev": "谷雨",
    "phase_curr": "立夏",
    "strength_change": "↑↑",
    "global_strength": "91.8",
    "strength_prev_week": "86.0",
    "strength_prev_month": "77.4",
    "labels": ["成交主力", "市值龙头"],
} == signal
```

Add two K-line supplement assertions: when any paid expansion value is missing,
`kline_supplement` contains `pullback_to_sma20`,
`breakout_20d_with_volume`, and `sma50_breakdown`; when all paid expansion
values are present, `kline_supplement is None`. These facts must not change
candidate eligibility or source actions.

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_a_share_trend.py::test_unified_trend_fields_match_the_paid_catalog_selection tests/test_market_trend.py -q
```

Expected: FAIL because `UNIFIED_TREND_FIELDS` and the new normalized fields do not exist.

- [ ] **Step 3: Implement the single shared field tuple and normalization**

In `a_share_trend.py`, replace the separate candidate/holding request tuples with:

```python
UNIFIED_TREND_FIELDS = (
    "tmId", "tickerName", "tickerSymbol", "asset", "asOfDate",
    "tradableFlag", "industryTmId", "industryName", "priceIndex",
    "marketCap", "amount1d", "isTrendRightSide",
    "trendTemperatureCurr", "trendTemperaturePrev",
    "daysSinceTrendEntry", "gainSinceTrendEntry",
    "trendPhasePrev", "trendPhaseCurr", "trendStrengthLocalCurr",
    "trendStrengthLocalChange", "trendStrengthGlobalCurr",
    "trendStrengthLocalPrevWeek", "trendStrengthLocalPrevMonth",
    "stopwinFlagByDangerSignal", "stopwinFlagByBoilingTemperature",
    "stopwinFlagByPopChampagne", "tickerLabels",
)
CANDIDATE_FIELDS = UNIFIED_TREND_FIELDS
HOLDING_FIELDS = UNIFIED_TREND_FIELDS
A_SHARE_SNAPSHOT_FIELDS = UNIFIED_TREND_FIELDS
```

Extend `CandidateInput` and `HoldingSnapshot` with nullable fields named `gain_since_entry`, `phase_prev`, `phase_curr`, `strength_change`, `global_strength`, `strength_prev_week`, `strength_prev_month`, `labels`, and `kline_supplement`. Parse numeric values with `_optional_decimal`, phase/strength markers as stripped strings, and labels with this helper:

```python
def _ticker_labels(value: object) -> tuple[str, ...]:
    if not isinstance(value, str):
        return ()
    return tuple(part.strip() for part in value.split(";") if part.strip())
```

Emit every field from both `_candidate_signal()` and `_holding_signal()`. Keep absent expansion fields as `None`/empty labels. Do not add expansion fields to `_candidate_reasons()`.

Add one shared `_kline_supplement()` using the already-fetched completed daily bars. It calculates SMA20, SMA50, the prior 20-day high, and relative volume against the prior 20 sessions, then returns only these booleans:

```python
{
    "pullback_to_sma20": sma20 > sma50 and low <= sma20 < close,
    "breakout_20d_with_volume": close > prior20_high and relative_volume >= Decimal("1.5"),
    "sma50_breakdown": close < sma50,
}
```

Call it only when at least one non-core paid expansion value is absent; otherwise store `None`. Reorder the existing holding K-line fetch before `_holding_snapshot()` so candidates and holdings use the same helper. This stays audit-only and must not enter `_candidate_reasons()`, `_holding_action()`, or option-attention category selection.

Change both CN and US/HK snapshot requests and cost calculations to use `UNIFIED_TREND_FIELDS`. Keep the existing billing-catalog check so an undeclared requested column fails before a paid request.

- [ ] **Step 4: Run focused report tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_a_share_trend.py tests/test_market_trend.py tests/test_trend_animals.py -q
```

Expected: PASS; API-fact assertions show the same unified field list for CN, HK, and US.

- [ ] **Step 5: Commit only the unified schema change**

```bash
git add src/open_trader/a_share_trend.py src/open_trader/market_trend.py tests/test_a_share_trend.py tests/test_market_trend.py
git commit -m "feat: unify trend animal snapshot fields"
```

---

### Task 2: Move the US trend account and watcher to Tiger

**Files:**
- Modify: `src/open_trader/market_trend.py:42-105,124-255,422-680`
- Modify: `src/open_trader/market_trend_watch.py:15-132`
- Modify: `src/open_trader/a_share_trend.py:606-665,791-983,1038-1070,1201-1260`
- Modify: `src/open_trader/cli.py:482-505,1297-1385`
- Test: `tests/test_market_trend.py`
- Test: `tests/test_market_trend_watch.py`
- Test: `tests/test_premarket_cli.py`

**Interfaces:**
- Produces: `load_trend_account(data_dir, market, expected_date, managed_symbols) -> AccountSnapshot`.
- Produces: `_refresh_tiger_account(config, run_date) -> None` using existing Tiger sync code.
- Extends: `estimate_buy_actions(ranked, net_value, available_cash, current_position_count, position_weight, market, lot_sizes, price_fx_to_account_currency) -> list[BuyAction]`.
- Consumes: `data/runs/<date>/tiger_account_snapshot.json` and Futu daily prices.

- [ ] **Step 1: Write failing Tiger ownership and HKD sizing tests**

Add tests that assert:

```python
assert market_paths(Path("data"), Path("reports"), "US").root == Path("data/trend_us_tiger")
assert MARKET_SETTINGS["US"]["broker"] == "tiger"
assert BROKER_LABELS["US"] == "老虎"
```

Create a Tiger snapshot fixture with one USD `account_total=100000`, USD available cash `10000`, HKD available cash `20000`, and one US stock. Assert `load_trend_account()` returns NAV `785000`, available cash `98500`, HKD position market value, and `fresh=True` only when the run-directory date equals `expected_date`.

Add a buy-sizing test:

```python
actions = estimate_buy_actions(
    ranked=[candidate(close="100", atr="5")],
    net_value=Decimal("785000"),
    available_cash=Decimal("98500"),
    current_position_count=0,
    position_weight=Decimal("0.04"),
    market="US",
    price_fx_to_account_currency=Decimal("7.85"),
)
assert actions[0].target_amount == Decimal("31400.00")
assert actions[0].estimated_shares == 40
```

Add a stale-account report test asserting no `BUY` action survives and every loaded holding has action `MANUAL_REVIEW`, reason `stale_tiger_account`.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_market_trend.py tests/test_market_trend_watch.py tests/test_premarket_cli.py -q
```

Expected: FAIL on `trend_us_tiger`, Tiger labels/account loading, and the new FX argument.

- [ ] **Step 3: Implement the Tiger adapter at the shared account seam**

Set US market ownership and paths directly:

```python
MARKET_SETTINGS = {
    "US": {"broker": "tiger", "currency": "HKD", "asset": "美股", "deadline": time(12)},
    "HK": {"broker": "phillips", "currency": "HKD", "asset": "港股", "deadline": time(19)},
}
MARKET_NOTIFICATION_LABELS = {
    "US": ("老虎", "美股", "确认 Trend Animals 与老虎账户状态后手动重跑老虎报告"),
    "HK": ("辉立", "港股", "确认 Trend Animals 与辉立日结单状态后手动重跑辉立报告"),
}
```

Use suffix `us_tiger` for US. Implement `_refresh_tiger_account()` by reusing `load_tiger_account_config`, `TigerAccountClient`, and `sync_tiger_portfolio` with `update_latest=True`, closing the client in `finally`.

Implement `load_tiger_trend_account()` against the newest valid `tiger_account_snapshot.json`. Require exactly one USD `account_total` row. Convert USD with `Decimal("7.85")`, HKD with `Decimal("1")`, and other currencies with the row's positive `fx_to_hkd`. Sum `min(cash_balance, available_balance)` across currency cash rows, clamp the final total at zero, and reject non-finite/missing required values. Include only positive ordinary US stock/ETF positions in `managed_symbols`.

Expose one dispatcher:

```python
def load_trend_account(*, data_dir: Path, market: str, expected_date: str,
                       managed_symbols: set[str]) -> AccountSnapshot:
    if _market(market) == "US":
        return load_tiger_trend_account(
            data_dir=data_dir,
            expected_date=expected_date,
            managed_symbols=managed_symbols,
        )
    return load_market_account(
        data_dir=data_dir,
        broker="phillips",
        market="HK",
        expected_date=expected_date,
        managed_symbols=managed_symbols,
    )
```

Use this dispatcher in both report generation and `watch_market_protection()`. Change `market_trend_watch.BROKER_LABELS["US"]` to `老虎`.

- [ ] **Step 4: Implement HKD sizing and stale-account safety**

Add `price_fx_to_account_currency: Decimal = Decimal("1")` to `estimate_buy_actions()` and calculate shares using:

```python
share_price = item.close * price_fx_to_account_currency
shares = int(amount / share_price / lot_size) * lot_size if lot_size > 0 else 0
```

Pass `Decimal("7.85")` for US and `Decimal("1")` elsewhere. Record `account_currency: "HKD"` and `price_fx_to_hkd: "7.85"` in US report metadata.

If the Tiger refresh fails, record the sanitized error and attempt the latest prior Tiger snapshot. If that snapshot is valid but stale, replace `report.buy_actions` with `()` and replace each holding with action `MANUAL_REVIEW`, reason `stale_tiger_account`. Add that reason to `REASON_LABELS` and render account status as `账户数据非实时，禁止新增买入；持仓需复核`. If no prior snapshot exists, let the report fail through the existing retry/failure-notification path.

Update CLI help text from “Futu US” to “Tiger US”; keep the generic `trend-market-report --market US` and `watch-trend-market --market US` commands so launchd templates need no new command.

- [ ] **Step 5: Verify and commit the Tiger seam**

Run:

```bash
.venv/bin/python -m pytest tests/test_market_trend.py tests/test_market_trend_watch.py tests/test_premarket_cli.py tests/test_a_share_trend.py -q
```

Expected: PASS, including the US failure title `【老虎｜美股趋势报告生成失败｜{report_date}】` and paths under `trend_us_tiger`.

```bash
git add src/open_trader/market_trend.py src/open_trader/market_trend_watch.py src/open_trader/a_share_trend.py src/open_trader/cli.py tests/test_market_trend.py tests/test_market_trend_watch.py tests/test_premarket_cli.py tests/test_a_share_trend.py
git commit -m "feat: move US trend reporting to Tiger"
```

---

### Task 3: Freeze deterministic option-attention transitions into source reports

**Files:**
- Modify: `src/open_trader/market_trend.py:300-680`
- Modify: `src/open_trader/a_share_trend.py:1169-1260`
- Test: `tests/test_market_trend.py`
- Test: `tests/test_a_share_trend.py`

**Interfaces:**
- Produces: `build_option_attention(current_rows, previous_rows, actions, market, broker_label) -> list[dict[str, object]]`.
- Produces: source-report JSON key `option_attention` for US/HK only.
- Consumes: adjacent valid frozen report `signal_snapshots`; falls back once to `data/trend_us_tiger/attention_baseline.json` only when `trend_us_tiger` has no predecessor.

- [ ] **Step 1: Write failing transition and Feishu tests**

Cover these cases in `tests/test_market_trend.py`:

```python
assert [item["symbol"] for item in attention] == ["DRAM", "QQQ"]
assert attention[0]["danger"] == {"previous": False, "current": True, "changed": True}
assert attention[1]["right_side"] == {"previous": False, "current": True, "changed": True}
assert attention[1]["days"] == 1
assert attention[1]["gain_since_entry"] == "0.048"
assert attention[1]["source_action"] == "BUY"
assert "headline" not in attention[1]
assert "summary" not in attention[1]
```

Also assert unchanged symbols are absent, a protection event alone adds nothing, a missing previous symbol enters only when `right_side is True` and `danger is False`, and missing expansion values remain `None`.

In `tests/test_a_share_trend.py`, assert Tiger/Phillips Feishu text appends one `期权关注` section with symbols and raw transitions, while CN text has no such section.

- [ ] **Step 2: Run tests and verify RED**

```bash
.venv/bin/python -m pytest tests/test_market_trend.py tests/test_a_share_trend.py -q
```

Expected: FAIL because source reports do not contain `option_attention`.

- [ ] **Step 3: Add the minimal adjacent-snapshot diff**

In `market_trend.py`, merge candidate and holding signal rows by normalized symbol, with holdings taking precedence. Compare only:

```python
ATTENTION_CHANGE_FIELDS = (
    "right_side", "temperature_curr", "phase_curr", "danger",
    "boiling", "champagne", "strength_change",
)
```

Each emitted item must always contain this fixed shape:

```python
{
    "market": market,
    "symbol": current["symbol"],
    "name": current.get("name"),
    "category": category,
    "right_side": transition("right_side"),
    "temperature": transition("temperature_curr"),
    "phase": transition("phase_curr"),
    "local_strength": current.get("strength"),
    "global_strength": current.get("global_strength"),
    "strength_prev_week": current.get("strength_prev_week"),
    "strength_prev_month": current.get("strength_prev_month"),
    "strength_change": transition("strength_change"),
    "days": current.get("days"),
    "gain_since_entry": current.get("gain_since_entry"),
    "danger": transition("danger"),
    "boiling": transition("boiling"),
    "champagne": transition("champagne"),
    "source_broker": broker_label,
    "source_action": actions.get(current["symbol"], "WATCH"),
}
```

`transition()` returns `previous`, `current`, and `changed`; no prose. Category is `risk` when a risk flag becomes true, `strengthened` when right-side becomes true or temperature rises, and `watch` otherwise.

Read the newest valid report with `as_of_date < current as_of_date`. Only when the new Tiger directory has none, read `paths.root / "attention_baseline.json"`, which Task 7 copies once from the newest 2026-07-15 legacy report. Never compare against a same-date revision. Attach the result to `payload["option_attention"]` before writing the delivery receipt. After the first Tiger report exists, ignore the baseline file.

- [ ] **Step 4: Append attention to the existing market Feishu message**

Extend `render_trend_feishu_text()` to read `option_attention` only for US/HK. Append lines such as:

```text
期权关注
1. QQQ｜右侧 否→是｜温度 温→热｜节气 谷雨→立夏
```

Include only changed fields, never protection lines or contract details. This stays inside the existing Tiger or Phillips daily message and therefore keeps existing daily-ledger deduplication.

- [ ] **Step 5: Verify and commit transition generation**

```bash
.venv/bin/python -m pytest tests/test_market_trend.py tests/test_a_share_trend.py tests/test_trend_delivery.py -q
```

Expected: PASS; unchanged snapshots emit an empty list and Feishu delivery count remains one per source market.

```bash
git add src/open_trader/market_trend.py src/open_trader/a_share_trend.py tests/test_market_trend.py tests/test_a_share_trend.py
git commit -m "feat: add cross-market option attention facts"
```

---

### Task 4: Project current, stale, and unavailable source states in Dashboard

**Files:**
- Modify: `src/open_trader/dashboard.py:60-460`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Produces: `trend_reports["tiger"]`, `trend_reports["phillips"]`, `trend_reports["eastmoney"]`, and aggregate `trend_reports["futu"]`.
- Produces: source `data_status` values `current`, `stale`, or `unavailable`.
- Consumes: frozen `option_attention` arrays from Task 3.

- [ ] **Step 1: Replace old fallback tests with the three-state contract**

Update `tests/test_dashboard.py` so a prior valid Tiger report asserts:

```python
assert report["available"] is True
assert report["data_status"] == "stale"
assert report["status_text"] == "数据截至 2026-07-14；今日未更新"
assert report["option_attention"] == stale_attention
```

Add tests that no valid report returns `available=False`, `data_status="unavailable"`, `status_text="暂时不可用"`; and a later same-day valid report wins over the stale report and returns `data_status="current"`.

Add an aggregate assertion:

```python
assert reports["futu"]["attention_markets"] == [
    {"market": "US", "market_label": "美股", "data_status": "stale",
     "data_date": "2026-07-14", "items": stale_us},
    {"market": "HK", "market_label": "港股", "data_status": "current",
     "data_date": "2026-07-15", "items": current_hk},
]
```

- [ ] **Step 2: Run Dashboard backend tests and verify RED**

```bash
.venv/bin/python -m pytest tests/test_dashboard.py -q
```

Expected: FAIL because Dashboard still rejects stale reports and maps US to Futu.

- [ ] **Step 3: Implement newest-valid source selection**

Change `TREND_REPORT_SOURCES` to Tiger/Phillips/Eastmoney:

```python
TREND_REPORT_SOURCES = {
    "tiger": ("US", "美股", "老虎", "trend_us_tiger", "美股常规交易时段"),
    "phillips": ("HK", "港股", "辉立", "trend_hk_phillips", "09:30–10:00"),
    "eastmoney": ("CN", "A股", "东方财富", "trend_a_share", "09:30–10:00"),
}
```

Read valid JSON reports newest-first, prefer `execution_date == report_date`, and otherwise select the newest earlier valid report. Reuse the existing structural validation for every candidate before selecting it; a malformed newest file must not hide an older valid file. Set stale status copy exactly as tested. Preserve actions from the stale file for display, but do not calculate or append any new attention in Dashboard.

Validate `option_attention` as a list of mappings with all fixed keys. Unknown/malformed attention makes that source report invalid rather than injecting arbitrary markup.

- [ ] **Step 4: Build the Futu projection without another artifact**

After loading the three source reports, build `trend_reports["futu"]` in memory from Tiger and Phillips:

```python
futu = {
    "available": any(source["available"] for source in (tiger, phillips)),
    "broker": "futu",
    "broker_label": "富途",
    "market": "US_HK",
    "market_label": "美股 / 港股",
    "status_text": "期权关注",
    "attention_markets": [project(tiger), project(phillips)],
}
```

`project()` always emits a market row, including unavailable rows with `items=[]`. Do not write a Futu report file or delivery ledger.

- [ ] **Step 5: Verify and commit Dashboard projection**

```bash
.venv/bin/python -m pytest tests/test_dashboard.py -q
```

Expected: PASS for current, stale, unavailable, malformed, and later-update selection.

```bash
git add src/open_trader/dashboard.py tests/test_dashboard.py
git commit -m "feat: project Futu option attention in dashboard"
```

---

### Task 5: Render the approved fixed-order attention list

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js:3-57,134-147,232-355,1900-1992,2207-2245,2372-2401`
- Modify: `src/open_trader/dashboard_static/dashboard.css:1450-1632,3969-4052`
- Test: `tests/test_dashboard_web.py`

**Interfaces:**
- Consumes: `trend_reports.futu.attention_markets` from Task 4.
- Produces: a deterministic table with the approved ten-column order and responsive labeled cells.

- [ ] **Step 1: Write failing frontend structure and mobile tests**

Add a Node VM rendering test with US and HK items. Assert the HTML column headings occur once and in this order:

```javascript
const headings = [
  "标的", "分类", "右侧状态", "趋势温度", "趋势节气",
  "本地 / 全球强度", "上周 / 上月", "右侧天数 / 累计涨幅",
  "危险 / 沸腾 / 开香槟", "来源动作",
];
```

Assert every row contains ten `data-label` cells in that same order; null values render `未提供`; US precedes HK; and output excludes `首次进入关注范围`, `危险信号首次出现`, `headline`, and `summary`.

Add CSS assertions that at `max-width: 760px` the table header is hidden, rows become two-column labeled cards, and the workspace has no horizontal scrolling; at `max-width: 460px` rows become one column.

- [ ] **Step 2: Run frontend tests and verify RED**

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py -q
```

Expected: FAIL because Futu has no attention workspace and Tiger has no report entry.

- [ ] **Step 3: Update account roles and entry labels**

Use these profiles:

```javascript
const ACCOUNT_STRATEGY_PROFILES = {
  futu: {horizon: "期权增强", strategy: "跨市场期权关注"},
  tiger: {horizon: "趋势", strategy: "美股趋势交易"},
  phillips: {horizon: "趋势", strategy: "港股趋势交易"},
  eastmoney: {horizon: "偏短线", strategy: "趋势交易"},
};
```

Allow entries for all four brokers. Label Futu's button `期权关注`; label the other three `当天趋势报告`. A stale source entry remains clickable and displays its exact status text. Remove the Tiger SMA200 strategy summary renderer and its gate labels.

- [ ] **Step 4: Implement the list renderer and native responsive CSS**

Add `renderOptionAttentionWorkspace(report)` and route only broker `futu` to it. Use one table and market separator rows. Format each transition directly from `{previous,current,changed}`; use `未提供` for null/empty values. Use deterministic action labels (`BUY` → `允许买入`, `SELL_ALL` → `卖出复核`, `HOLD` → `继续持有`, everything else → `观察`).

The row cell order must be the heading order from Step 1. Apply emphasis only when `changed === true`; do not synthesize a sentence above the fields.

Reuse the existing native responsive table pattern: CSS grid, `data-label`, and media queries. Do not add JavaScript layout measurement or a UI dependency.

- [ ] **Step 5: Verify and commit the approved UI**

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py tests/test_dashboard.py -q
```

Expected: PASS for desktop/mobile rendering, keyboard open/close flow, stale copy, and fixed field order.

```bash
git add src/open_trader/dashboard_static/dashboard.js src/open_trader/dashboard_static/dashboard.css tests/test_dashboard_web.py tests/test_dashboard.py
git commit -m "feat: render unified option attention list"
```

---

### Task 6: Retire Tiger SMA200 and old Futu-US code paths

**Files:**
- Delete: `src/open_trader/tiger_long_term.py`
- Delete: `src/open_trader/tiger_long_term_backtest.py`
- Delete: `config/tiger_long_term_strategy.json`
- Delete: `tests/test_tiger_long_term.py`
- Delete: `tests/test_tiger_long_term_backtest.py`
- Modify: `src/open_trader/daily_premarket.py:36-42,472-508,612-660,703-735,908-919,932-976,2177-2229`
- Modify: `src/open_trader/cli.py:96-105,806-850,1934-1965`
- Modify: `src/open_trader/dashboard.py:60-160,260-295`
- Modify: `src/open_trader/dashboard_acceptance.py`
- Modify: `tests/test_daily_premarket.py`
- Modify: `tests/test_pipeline.py`
- Modify: `tests/test_dashboard.py`
- Modify: `tests/test_dashboard_acceptance.py`
- Modify: `README.md`
- Modify: `README.zh-CN.md`

**Interfaces:**
- Removes: `run-tiger-long-term-strategy`, `generate_tiger_long_term_strategy`, `tiger_long_term_strategy` Dashboard payload, and all SMA200 status reasons/artifacts.
- Preserves: Tiger account sync, Tiger holdings/NAV, and the new Tiger US trend report.

- [ ] **Step 1: Change tests to assert complete retirement**

Replace long-term CLI/pipeline tests with assertions that:

```python
with pytest.raises(SystemExit):
    build_parser().parse_args(["run-tiger-long-term-strategy"])

assert "tiger_long_term_strategy" not in load_dashboard_state(config).to_dict()
assert "tiger_long_term_strategy_failed" not in status["status_reasons"]
```

Update acceptance fixtures to require a Tiger trend entry and a Futu attention entry, and to reject `SMA200 策略`, `SMA200 组合策略`, and the old Futu US trend identity.

- [ ] **Step 2: Run affected tests and verify RED**

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py tests/test_pipeline.py tests/test_dashboard.py tests/test_dashboard_acceptance.py -q
```

Expected: FAIL while SMA200 imports, CLI, artifacts, and Dashboard payload remain.

- [ ] **Step 3: Delete the SMA200 implementation and remove every caller**

Delete the two implementation modules, their dedicated tests, and `config/tiger_long_term_strategy.json`. Remove:

- the import and injected generator from `DailyPremarketRunner`;
- the US `_run_locked` shadow-strategy block;
- the four `tiger_long_term_*` artifact keys;
- the `tiger_long_term_strategy_failed` argument and status reason;
- the CLI parser, dispatch branch, and import;
- the Dashboard dataclass field, loader, API key, and import.

Do not alter Tiger account synchronization. Do not leave compatibility aliases or a disabled feature flag; Git is the rollback history.

- [ ] **Step 4: Update acceptance and user documentation**

Change Dashboard acceptance mappings from `futu: trend_us_futu` to `tiger: trend_us_tiger`, add the Futu aggregate UI assertions from Task 5, and keep Phillips/Eastmoney mappings unchanged. Update README command examples and account-role text to name Tiger US and Futu options attention. Remove all active SMA200 instructions.

- [ ] **Step 5: Verify absence and commit the deletion**

Run:

```bash
rg -n "tiger_sma200|tiger_long_term|SMA200 组合策略|trend_us_futu" src tests config README.md README.zh-CN.md
.venv/bin/python -m pytest tests/test_daily_premarket.py tests/test_pipeline.py tests/test_dashboard.py tests/test_dashboard_web.py tests/test_dashboard_acceptance.py -q
```

Expected: `rg` exits 1 with no matches; pytest PASS.

```bash
git add -A src/open_trader/tiger_long_term.py src/open_trader/tiger_long_term_backtest.py config/tiger_long_term_strategy.json tests/test_tiger_long_term.py tests/test_tiger_long_term_backtest.py src/open_trader/daily_premarket.py src/open_trader/cli.py src/open_trader/dashboard.py src/open_trader/dashboard_acceptance.py tests/test_daily_premarket.py tests/test_pipeline.py tests/test_dashboard.py tests/test_dashboard_web.py tests/test_dashboard_acceptance.py README.md README.zh-CN.md
git commit -m "refactor: retire Tiger SMA200 strategy"
```

---

### Task 7: Migrate live state, verify real workflows, and pass the acceptance gate

**Files:**
- Runtime migration: `data/trend_us_futu/`, `reports/trend_us_futu/`, `data/latest/US/tiger_long_term_strategy.json`, dated Tiger long-term artifacts
- Runtime outputs: `data/trend_us_tiger/`, `reports/trend_us_tiger/`, HK/CN report revisions, logs
- No source edits after the accepted commit

**Interfaces:**
- Consumes: all code from Tasks 1-6 and the 2026-07-15 Futu report as one-time diff baseline.
- Produces: live Tiger US report/watcher, updated HK/CN unified fields, accepted Dashboard process, and review URL.

- [ ] **Step 1: Run focused and full automated verification**

Run:

```bash
.venv/bin/python -m pytest tests/test_trend_animals.py tests/test_a_share_trend.py tests/test_market_trend.py tests/test_market_trend_watch.py tests/test_premarket_cli.py tests/test_dashboard.py tests/test_dashboard_web.py tests/test_dashboard_acceptance.py -q
make test
```

Expected: both commands PASS. Do not run `make acceptance` yet.

- [ ] **Step 2: Commit the confirmed spec/plan and record the candidate SHA**

```bash
git add docs/superpowers/specs/2026-07-16-tiger-trend-futu-options-attention-design.md docs/superpowers/plans/2026-07-16-tiger-trend-futu-options-attention.md
git commit -m "docs: define Tiger trend and Futu attention rollout"
git status --short
git rev-parse HEAD
```

Expected: only pre-existing unrelated user files may remain untracked/modified; record the printed SHA as the candidate SHA.

- [ ] **Step 3: Stop old in-memory jobs and run the real report workflows**

Inspect before changing processes:

```bash
screen -ls
launchctl list | rg 'com\.open-trader\.(trend|premarket)'
ps -axo pid,lstart,command | rg 'open_trader (dashboard|trend-market-report|watch-trend-market)'
```

Reload the US and HK trend jobs from the current repository:

```bash
scripts/install_daily_premarket_launchd.sh --trend-only --market US
scripts/install_daily_premarket_launchd.sh --trend-only --market HK
```

Before the first Tiger US report, copy the newest 2026-07-15 Futu report as the
one-time read-only baseline:

```bash
mkdir -p data/trend_us_tiger
baseline="$(find reports/trend_us_futu -maxdepth 1 -type f -name '2026-07-15*.json' -print | sort | tail -n 1)"
test -n "$baseline"
cp "$baseline" data/trend_us_tiger/attention_baseline.json
```

Then run current data directly:

```bash
PYTHONPATH=src .venv/bin/python -m open_trader trend-market-report --market US --date today --config config/daily_premarket.env
PYTHONPATH=src .venv/bin/python -m open_trader trend-market-report --market HK --date today --revision --config config/daily_premarket.env
PYTHONPATH=src .venv/bin/python -m open_trader trend-a-share-report --date today --revision --config config/daily_premarket.env
```

Expected: US reports under `reports/trend_us_tiger`; HK/CN revisions contain all unified snapshot keys; notification ledgers still contain one semantic daily message per source market.

- [ ] **Step 4: Remove retired runtime artifacts only after the new US report exists**

First prove the replacement exists and the 2026-07-15 baseline was consumed:

```bash
test -n "$(find reports/trend_us_tiger -type f -name '*.json' -print -quit)"
rg -n '"option_attention"|"broker": "tiger"' reports/trend_us_tiger/*.json
```

Then remove the explicitly retired artifacts:

```bash
rm -rf data/trend_us_futu reports/trend_us_futu
rm -f data/trend_us_tiger/attention_baseline.json
rm -f data/latest/US/tiger_long_term_strategy.json
find data/runs -path '*/US/tiger_long_term_strategy.json' -delete
```

Expected: the old paths no longer exist; `data/trend_us_tiger` protection state and logs remain.

- [ ] **Step 5: Start the candidate Dashboard and inspect fresh process evidence**

```bash
screen -S open_trader_dashboard_8766 -X quit 2>/dev/null || true
screen -dmS open_trader_dashboard_8766 zsh -lc 'cd /Users/ray/projects/open_trader && export PYTHONPATH=src && exec .venv/bin/python -m open_trader dashboard --portfolio data/latest/portfolio.csv --data-dir data --reports-dir reports --poll-seconds 5 --host 127.0.0.1 --port 8766 >>/tmp/open_trader_dashboard_8766.log 2>&1'
sleep 2
screen -ls | rg 'open_trader_dashboard_8766'
ps -axo pid,lstart,command | rg 'open_trader dashboard.*8766'
tail -n 50 /tmp/open_trader_dashboard_8766.log
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/
```

Expected: a new PID/start time, repository working directory in the command, fresh logs, and HTTP `200`.

- [ ] **Step 6: Run the final Dashboard acceptance gate once**

```bash
make acceptance
```

Expected: final line/result `PASS`. On `FAIL`, fix the cause, rerun focused/direct checks, and repeat this step. On `BLOCKED`, stop and report the external blocker; do not substitute curl, fixtures, mocks, or screenshots.

- [ ] **Step 7: Redeploy the exact accepted SHA and provide the review URL**

```bash
accepted_sha="$(git rev-parse HEAD)"
screen -S open_trader_dashboard_8766 -X quit 2>/dev/null || true
screen -dmS open_trader_dashboard_8766 zsh -lc 'cd /Users/ray/projects/open_trader && export PYTHONPATH=src && exec .venv/bin/python -m open_trader dashboard --portfolio data/latest/portfolio.csv --data-dir data --reports-dir reports --poll-seconds 5 --host 127.0.0.1 --port 8766 >>/tmp/open_trader_dashboard_8766.log 2>&1'
sleep 2
test "$(git rev-parse HEAD)" = "$accepted_sha"
screen -ls | rg 'open_trader_dashboard_8766'
ps -axo pid,lstart,command | rg 'open_trader dashboard.*8766'
tail -n 50 /tmp/open_trader_dashboard_8766.log
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/
```

Expected: same accepted SHA, a post-acceptance PID/start time, fresh logs, and HTTP `200`. Hand off `http://127.0.0.1:8766/` only after these checks.
