# A 股趋势纪律与 Dashboard 报告重设计 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将批准的 12 条 A 股入场纪律、温度转平退出、保护线上移、持仓提示和动作优先 Dashboard 落到现有东方财富报告链路，并用真实数据原位重生成 `2026-07-15` 报告。

**Architecture:** 继续以 `a_share_trend.py` 作为 A 股规则和冻结报告的唯一事实源，在现有候选/持仓数据类上做向后兼容的加法扩展；美股和港股继续走原有默认分支。Dashboard 只投影并展示冻结 JSON，不调用 Trend Animals、不重复计算纪律；前端只为 `market === "CN"` 选择新布局，其他市场沿用现有渲染器。

**Tech Stack:** Python 3.12、标准库 `dataclasses`/`decimal`/`pathlib`、pytest、原生 JavaScript、CSS、现有 Trend Animals 与 Futu 客户端、Playwright acceptance。

## Global Constraints

- 仅修改东方财富 A 股趋势语义；富途美股、辉立港股和老虎策略保持原样。
- 不增加依赖、前端框架、主题、图标库、schema version 或自动下单能力。
- A 股正式入场：`温 → 热` 目标仓位 4%，`温 → 沸` 目标仓位 2%。
- A 股边界均包含：`priceIndex <= 200`、强度 `>= 95`、市值 `>= 100` 亿元、日成交额 `>= 2` 亿元。
- 允许节气只有 `谷雨`、`立夏`、`夏至`；行业温度只有 `热`、`沸`。
- `右侧天数 < 10` 不再是 A 股门槛，但右侧天数缺失仍因无法确定排序而排除。
- `沸腾`/`开香槟` 只按 `max(旧保护线, 前 5 日最低价)` 上移保护线，不减仓。
- 持仓退出优先级：保护线触发、危险信号、离开右侧、`温/热/沸 → 平`、继续持有。
- 买入纪律失效只生成持仓提示，不自动卖出。
- 行业 ID 去重后只发一次批量行业快照请求；整体失败阻断报告，单个行业缺行只排除相关候选。
- 主表只包含现金和席位约束后可执行的正式动作；完整候选事实与排除原因留在审计 JSON。
- `筛选价` 固定来自 Trend Animals `priceIndex`；`执行参考价` 固定来自 Futu 数据日前复权收盘价。
- 一次性重生成使用 `NullNotifier`，不创建 revision、不保留旧报告副本、不增加通用覆盖开关。
- 每个代码任务提交后重启 Dashboard 并运行 `make acceptance`；只有 `PASS` 才进入下一任务。
- 最终 `PASS` 后必须再次部署完全相同的 Git SHA，并验证新 PID、cwd、SHA、新日志和 HTTP 200。

---

## File Map

- Modify: `src/open_trader/a_share_trend.py` — A 股快照字段、纪律、仓位、持仓动作、审计 JSON、Markdown 和真实行业批量请求。
- Modify: `tests/test_a_share_trend.py` — A 股边界、退出优先级、行业批量请求、两个价格、审计和美港兼容回归。
- Modify: `src/open_trader/dashboard.py` — 将冻结 JSON 中的 A 股完整审计事实投影给前端，保留其他市场投影。
- Modify: `tests/test_dashboard.py` — 验证 A 股新增投影及美港兼容。
- Modify: `src/open_trader/dashboard_static/dashboard.js` — 仅 A 股动作优先桌面表格、移动事实卡、纪律和审计展示。
- Modify: `src/open_trader/dashboard_static/dashboard.css` — 复用现有 token 的 A 股布局和 375px 无横向滚动规则。
- Modify: `tests/test_dashboard_web.py` — 原生 JS 渲染、安全转义、桌面顺序和移动布局回归。
- Modify: `src/open_trader/dashboard_acceptance.py` — 真实浏览器检查 A 股表格/卡片、纪律折叠和审计投影。
- Modify: `tests/test_dashboard_acceptance.py` — acceptance helper 的确定性单测。
- Runtime-only overwrite: `reports/trend_a_share/2026-07-15.{json,md}` and matching ignored state/receipt files — 由现有私有 `_attempt_report` 原位替换，不提交到 Git。

---

### Task 1: A 股规则、数据流与冻结报告

**Files:**
- Modify: `src/open_trader/a_share_trend.py:31-58,137-218,377-827,837-1245,1841-2110`
- Test: `tests/test_a_share_trend.py:52-175,200-910,1685-1845,2030-2200,3240-3355`

**Interfaces:**
- Consumes: existing `TrendAnimalsClient.get_snapshots(tm_ids, fields, expected_date)`, `FutuQuoteClient.get_daily_kline(...)`, `build_report(...)`, `_report_payload(...)`, and `_freeze_receipt_report(...)`.
- Produces: additive candidate/holding/action JSON fields consumed by Task 2: `filter_price`, `close`（执行参考价）, `market_cap`, `industry_tm_id`, `industry_temperature`, `temperature_prev`, `temperature_curr`, `phase`, `target_weight`, `entry_hints`, plus `signal_snapshots.candidates[*].eligible/excluded_reasons/rank`.

- [ ] **Step 1: Extend test builders with valid A 股 discipline defaults**

Update the `candidate()` and `holding()` test helpers so unrelated existing tests remain valid while new tests can override every added fact:

```python
def candidate(
    symbol: str,
    *,
    strength: str | None = "96",
    days: int | None = 3,
    amount: str | None = "2",
    right_side: object = True,
    tradable: object = True,
    danger: object = False,
    exchange: str = "SH",
    name: str | None = None,
    close: str = "10",
    atr: str | None = "0.5",
    industry: str = "电力",
    industry_tm_id: int | None = 700001,
    industry_temperature: str | None = "热",
    filter_price: str | None = "10",
    market_cap: str | None = "100",
    temperature_prev: str | None = "温",
    temperature_curr: str | None = "热",
    phase: str | None = "立夏",
    asset: str = "A股",
) -> CandidateInput:
    return CandidateInput(
        tm_id=int(symbol),
        symbol=symbol,
        exchange=exchange,
        name=f"股票{symbol}" if name is None else name,
        asset=asset,
        industry=industry,
        as_of_date="2026-07-14",
        tradable=tradable,
        amount=None if amount is None else Decimal(amount),
        right_side=right_side,
        days=days,
        strength=None if strength is None else Decimal(strength),
        danger=danger,
        close=Decimal(close),
        atr=None if atr is None else Decimal(atr),
        industry_tm_id=industry_tm_id,
        industry_temperature=industry_temperature,
        filter_price=None if filter_price is None else Decimal(filter_price),
        market_cap=None if market_cap is None else Decimal(market_cap),
        temperature_prev=temperature_prev,
        temperature_curr=temperature_curr,
        phase=phase,
    )


def holding(
    symbol: str,
    *,
    right_side: bool | None = True,
    danger: bool | None = False,
    boiling: bool | None = False,
    champagne: bool | None = False,
    industry: str = "电力",
    industry_tm_id: int | None = 700001,
    industry_temperature: str | None = "热",
    filter_price: str | None = "10",
    market_cap: str | None = "100",
    strength: str | None = "96",
    temperature_prev: str | None = "温",
    temperature_curr: str | None = "热",
    phase: str | None = "立夏",
) -> HoldingSnapshot:
    return HoldingSnapshot(
        tm_id=int(symbol),
        symbol=symbol,
        exchange="SH",
        name=f"股票{symbol}",
        as_of_date="2026-07-14",
        right_side=right_side,
        danger=danger,
        boiling=boiling,
        champagne=champagne,
        industry=industry,
        industry_tm_id=industry_tm_id,
        industry_temperature=industry_temperature,
        filter_price=None if filter_price is None else Decimal(filter_price),
        market_cap=None if market_cap is None else Decimal(market_cap),
        strength=None if strength is None else Decimal(strength),
        temperature_prev=temperature_prev,
        temperature_curr=temperature_curr,
        phase=phase,
    )
```

- [ ] **Step 2: Write failing discipline and boundary tests**

Add focused tests covering all twelve gates, inclusive boundaries, allowed phases/industry temperatures, missing-field fail-closed behavior, and removal of the old day threshold:

```python
@pytest.mark.parametrize("phase", ["谷雨", "立夏", "夏至"])
@pytest.mark.parametrize("industry_temperature", ["热", "沸"])
def test_cn_candidate_accepts_allowed_phase_and_industry_temperature(
    phase: str, industry_temperature: str,
) -> None:
    item = candidate(
        "600001", phase=phase, industry_temperature=industry_temperature,
        filter_price="200", strength="95", market_cap="100", amount="2",
        days=15,
    )
    assert build_candidate_list([item], held_symbols=set()).eligible == (item,)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"asset": "ETF基金"}, "a_share_only"),
        ({"temperature_prev": "平"}, "temperature_transition_not_entry"),
        ({"temperature_curr": "温"}, "temperature_transition_not_entry"),
        ({"filter_price": Decimal("200.01")}, "filter_price_above_200"),
        ({"strength": Decimal("94.99")}, "strength_below_95"),
        ({"industry_temperature": "温"}, "industry_temperature_not_hot"),
        ({"phase": "小暑"}, "phase_after_summer_solstice"),
        ({"market_cap": Decimal("99.99")}, "market_cap_below_100"),
        ({"amount": Decimal("1.99")}, "amount_below_2"),
        ({"exchange": "BJ"}, "excluded_security"),
    ],
)
def test_cn_candidate_rejects_failed_discipline(
    changes: dict[str, object], reason: str,
) -> None:
    decision = build_candidate_list(
        [replace(candidate("600001"), **changes)], held_symbols=set()
    )
    assert reason in decision.excluded["600001"]


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("temperature_prev", "temperature_missing"),
        ("temperature_curr", "temperature_missing"),
        ("filter_price", "filter_price_missing"),
        ("strength", "strength_missing"),
        ("industry_tm_id", "industry_id_missing"),
        ("industry_temperature", "industry_temperature_missing"),
        ("phase", "phase_missing"),
        ("market_cap", "market_cap_missing"),
        ("amount", "amount_missing"),
        ("days", "right_side_days_missing"),
    ],
)
def test_cn_candidate_missing_required_fact_is_excluded(
    field: str, reason: str,
) -> None:
    decision = build_candidate_list(
        [replace(candidate("600001"), **{field: None})], held_symbols=set()
    )
    assert reason in decision.excluded["600001"]
```

Keep the existing HK/US compatibility test and change only its fixture inputs to the old valid thresholds; assert it still accepts ETF assets and does not require the new A 股-only fields.

- [ ] **Step 3: Run the new rule tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_a_share_trend.py::test_cn_candidate_accepts_allowed_phase_and_industry_temperature \
  tests/test_a_share_trend.py::test_cn_candidate_rejects_failed_discipline \
  tests/test_a_share_trend.py::test_cn_candidate_missing_required_fact_is_excluded
```

Expected: FAIL because `CandidateInput` does not yet accept the new facts and the old A 股 thresholds/day rule are still active.

- [ ] **Step 4: Add the minimum A 股-only facts and rule branch**

Keep the existing shared constants unchanged for HK/US and add only these A 股 request sets:

```python
A_SHARE_DISCIPLINE_FIELDS = (
    "industryTmId",
    "priceIndex",
    "marketCap",
    "trendTemperatureCurr",
    "trendTemperaturePrev",
    "trendPhaseCurr",
)
A_SHARE_SNAPSHOT_FIELDS = HOLDING_FIELDS + A_SHARE_DISCIPLINE_FIELDS
A_SHARE_INDUSTRY_FIELDS = (
    "tmId",
    "asOfDate",
    "trendTemperatureCurr",
)
ALLOWED_ENTRY_PHASES = {"谷雨", "立夏", "夏至"}
HOT_TEMPERATURES = {"热", "沸"}
KNOWN_TEMPERATURES = {"凉", "平", "温", "热", "沸"}
```

Add optional fields to the existing frozen dataclasses; do not create parallel CN-only models:

```python
@dataclass(frozen=True)
class CandidateInput:
    tm_id: int
    symbol: str
    exchange: str
    name: str
    asset: str
    industry: str
    as_of_date: str
    tradable: object
    amount: Decimal | None
    right_side: object
    days: int | None
    strength: Decimal | None
    danger: object
    close: Decimal | None
    atr: Decimal | None
    pools: tuple[str, ...] = ()
    industry_tm_id: int | None = None
    industry_temperature: str | None = None
    filter_price: Decimal | None = None
    market_cap: Decimal | None = None
    temperature_prev: str | None = None
    temperature_curr: str | None = None
    phase: str | None = None
```

Read those fields in `evaluate_candidate`, accepting `industry_temperature` as a keyword supplied by the batch lookup. Preserve the current `_candidate_reasons` body for `market != "CN"`; for CN use this discipline block before the existing common safety checks:

```python
if market == "CN":
    if item.asset != "A股":
        reasons.append("a_share_only")
    if item.temperature_prev is None or item.temperature_curr is None:
        reasons.append("temperature_missing")
    elif item.temperature_prev != "温" or item.temperature_curr not in HOT_TEMPERATURES:
        reasons.append("temperature_transition_not_entry")
    if item.filter_price is None:
        reasons.append("filter_price_missing")
    elif item.filter_price > 200:
        reasons.append("filter_price_above_200")
    if item.strength is None:
        reasons.append("strength_missing")
    elif item.strength < 95:
        reasons.append("strength_below_95")
    if item.industry_tm_id is None:
        reasons.append("industry_id_missing")
    if item.industry_temperature is None:
        reasons.append("industry_temperature_missing")
    elif item.industry_temperature not in HOT_TEMPERATURES:
        reasons.append("industry_temperature_not_hot")
    if item.phase is None:
        reasons.append("phase_missing")
    elif item.phase not in ALLOWED_ENTRY_PHASES:
        reasons.append("phase_after_summer_solstice")
    if item.market_cap is None:
        reasons.append("market_cap_missing")
    elif item.market_cap < 100:
        reasons.append("market_cap_below_100")
    if item.amount is None:
        reasons.append("amount_missing")
    elif item.amount < 2:
        reasons.append("amount_below_2")
    if item.days is None:
        reasons.append("right_side_days_missing")
```

Then reuse the existing right-side, tradable, danger, identity, held-symbol, ST/delisting, exchange, ATR, and date checks. Do not append `right_side_days_not_below_10`, `strength_not_above_90`, or `amount_below_1` in the CN branch.

- [ ] **Step 5: Write failing per-candidate sizing, exit, hint, and protection tests**

```python
def test_cn_buy_weight_follows_current_temperature() -> None:
    actions = estimate_buy_actions(
        ranked=[
            candidate("600001", temperature_curr="热"),
            candidate("600002", temperature_curr="沸"),
        ],
        net_value=Decimal("100000"),
        available_cash=Decimal("10000"),
        current_position_count=0,
        position_weight=Decimal("0.04"),
    )
    assert [
        (item.symbol, item.target_weight, item.target_amount, item.estimated_shares)
        for item in actions
    ] == [
        ("600001", Decimal("0.04"), Decimal("4000.00"), 400),
        ("600002", Decimal("0.02"), Decimal("2000.00"), 200),
    ]


@pytest.mark.parametrize("previous", ["温", "热", "沸"])
def test_cn_holding_temperature_transition_to_flat_sells(previous: str) -> None:
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account("600001"),
        candidates=(),
        holding_snapshots={
            "600001": holding(
                "600001", temperature_prev=previous, temperature_curr="平"
            )
        },
        bars_by_symbol={"600001": bars()},
    )
    assert (built.holdings[0].action, built.holdings[0].reason) == (
        "SELL_ALL", "temperature_changed_to_flat"
    )


def test_cn_holding_continuous_flat_does_not_create_temperature_sell() -> None:
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account("600001"),
        candidates=(),
        holding_snapshots={
            "600001": holding(
                "600001", temperature_prev="平", temperature_curr="平"
            )
        },
        bars_by_symbol={"600001": bars()},
    )
    assert built.holdings[0].action == "HOLD"


def test_cn_holding_entry_failures_are_hints_not_sell_triggers() -> None:
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account("600001"),
        candidates=(),
        holding_snapshots={
            "600001": holding(
                "600001", strength="91.3", phase="小暑",
                temperature_prev="热", temperature_curr="热",
            )
        },
        bars_by_symbol={"600001": bars()},
    )
    decision = built.holdings[0]
    assert decision.action == "HOLD"
    assert decision.entry_hints == (
        "强度 91.3，低于入场线 95",
        "节气已到小暑",
        "不是新的温转热或温转沸入场信号",
    )


def test_cn_boiling_and_champagne_never_create_trim_action() -> None:
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account("600001"),
        candidates=(),
        holding_snapshots={
            "600001": holding("600001", boiling=True, champagne=True)
        },
        bars_by_symbol={"600001": bars(close=12, low=11)},
        prior_state={
            "schema_version": 1,
            "positions": {"600001": {
                "initial_line": "8", "active_line": "9", "atr14": "1",
                "tracking_active": False, "updated_for": "2026-07-13",
            }},
        },
    )
    assert built.holdings[0].action == "HOLD"
    assert built.holdings[0].active_line == Decimal("11")
```

Also add a precedence parametrization asserting protection, danger, and right-side exit each beat the temperature transition, and that a missing current/previous temperature produces `MANUAL_REVIEW / holding_signal_unknown` only after the three stronger sell gates.

- [ ] **Step 6: Run sizing and holding tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_a_share_trend.py \
  -k 'cn_buy_weight or cn_holding_temperature or cn_holding_entry or cn_boiling'
```

Expected: FAIL because `BuyAction.target_weight`, temperature exit, and `entry_hints` do not exist.

- [ ] **Step 7: Implement per-candidate sizing and holding facts in the shared path**

Add `target_weight` and self-contained display facts to `BuyAction`; add `close`, temperature facts, strength, and `entry_hints` to `HoldingDecision`. Compute the CN target inside the existing `estimate_buy_actions` loop:

```python
weight = (
    {"热": Decimal("0.04"), "沸": Decimal("0.02")}.get(
        item.temperature_curr
    )
    if market == "CN"
    else position_weight
)
if weight is None:
    continue
target = (net_value * weight).quantize(Decimal("0.01"))
```

Pass `market` into `_holding_action(...)`. Keep the first three branches unchanged, then for CN add `temperature_changed_to_flat`, then require known previous/current temperatures before returning `HOLD`. Build `entry_hints` in one private `_holding_entry_hints(snapshot)` helper; this is the only place that translates failed entry facts into display strings.

- [ ] **Step 8: Write failing industry batch and audit tests**

Extend `ReadyApi` so it records every snapshot request as `(tm_ids, fields)` and returns industry rows when `fields == A_SHARE_INDUSTRY_FIELDS`. Add:

```python
self.snapshot_requests: list[tuple[list[int], tuple[str, ...]]] = []
self.missing_industry_ids = missing_industry_ids or set()
self.industry_error = industry_error
```

The security rows must include these valid facts so existing runner tests remain green:

```python
"industryTmId": 700001,
"priceIndex": "10",
"marketCap": "100",
"trendTemperaturePrev": "温",
"trendTemperatureCurr": "热",
"trendPhaseCurr": "立夏",
```

For the industry request, return one row per requested ID not in `missing_industry_ids` with `tmId`, `asOfDate`, and `trendTemperatureCurr: "热"`; raise `industry_error` before returning when it is set.

```python
def test_report_runner_fetches_unique_industries_in_one_batch(tmp_path: Path) -> None:
    calls: list[str] = []
    api = ReadyApi(calls)
    result = run_a_share_trend_report(
        config=trend_config(tmp_path),
        run_date="2026-07-14",
        api_factory=lambda **kwargs: api,
        quote_factory=lambda **kwargs: ReadyQuote(calls),
        notifier=RecordingFeishu(),
    )
    assert api.snapshot_requests == [
        ([1, 2], A_SHARE_SNAPSHOT_FIELDS),
        ([700001], A_SHARE_INDUSTRY_FIELDS),
    ]
    payload = json.loads(result.json_path.read_text(encoding="utf-8"))
    audit = payload["signal_snapshots"]["candidates"]
    assert audit[0]["industry_tm_id"] == 700001
    assert audit[0]["industry_temperature"] == "热"


def test_missing_industry_row_excludes_only_affected_candidate(
    tmp_path: Path,
) -> None:
    api = ReadyApi([], missing_industry_ids={700001})
    result = run_a_share_trend_report(
        config=trend_config(tmp_path), run_date="2026-07-14",
        api_factory=lambda **kwargs: api,
        quote_factory=lambda **kwargs: ReadyQuote([]),
        notifier=RecordingFeishu(),
    )
    payload = json.loads(result.json_path.read_text(encoding="utf-8"))
    assert payload["excluded"] == {
        "000001": ["industry_temperature_missing"],
        "000002": ["industry_temperature_missing"],
    }


def test_industry_snapshot_failure_blocks_report(tmp_path: Path) -> None:
    result = run_a_share_trend_report(
        config=trend_config(tmp_path), run_date="2026-07-14",
        now_fn=lambda: datetime(2026, 7, 14, 18, 0, tzinfo=SHANGHAI),
        api_factory=lambda **kwargs: ReadyApi(
            [], industry_error=TrendAnimalsError("industry unavailable")
        ),
        quote_factory=lambda **kwargs: ReadyQuote([]),
        notifier=RecordingMacOS(),
    )
    assert result.status == "failed"
    assert not list((tmp_path / "reports").rglob("*.json"))
```

- [ ] **Step 9: Run industry tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_a_share_trend.py \
  -k 'unique_industries or missing_industry_row or industry_snapshot_failure'
```

Expected: FAIL because the report runner currently issues only one security snapshot request and has no industry temperature join.

- [ ] **Step 10: Implement the single industry batch and additive audit facts**

In `_attempt_report`, use `A_SHARE_SNAPSHOT_FIELDS`, validate their billing rows, request the security snapshots, then join one industry batch with this shape:

```python
industry_ids = sorted({
    value for row in snapshot_rows
    if isinstance((value := row.get("industryTmId")), int)
    and not isinstance(value, bool) and value > 0
})
industry_rows = (
    api.get_snapshots(
        tm_ids=industry_ids,
        fields=A_SHARE_INDUSTRY_FIELDS,
        expected_date=run_date,
    )
    if industry_ids else []
)
returned_industry_ids = [_row_tm_id(row) for row in industry_rows]
if (
    len(returned_industry_ids) != len(set(returned_industry_ids))
    or any(tm_id not in industry_ids for tm_id in returned_industry_ids)
):
    raise TrendAnimalsError("industry snapshot returned mismatched tmIds")
industry_temperatures = {
    _row_tm_id(row): (
        str(row.get("trendTemperatureCurr"))
        if row.get("trendTemperatureCurr") in HOT_TEMPERATURES
        else None
    )
    for row in industry_rows
}
```

Missing industry rows remain absent from the map and therefore exclude only affected candidates; `TrendAnimalsError` from the whole call propagates and blocks the report. Pass the joined temperature into `evaluate_candidate` and `_holding_snapshot`. Calculate estimated cost as security-field cost times security IDs plus industry-field cost times unique industry IDs.

Extend `_candidate_signal(...)`, `_holding_signal(...)`, serialized actions, and CN Markdown with the two named prices and all audit facts. Keep `report.candidates` as the ranked eligible list for backward compatibility; `signal_snapshots.candidates` remains the complete raw candidate ledger with `eligible`, `excluded_reasons`, and `rank`.

- [ ] **Step 11: Run the complete backend test file**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_a_share_trend.py
```

Expected: PASS, including existing HK/US behavior, delivery recovery, atomic freeze, and notification tests.

- [ ] **Step 12: Commit, restart the exact SHA, and run the repository gate**

```bash
git add src/open_trader/a_share_trend.py tests/test_a_share_trend.py
git commit -m "feat: apply A-share trend discipline"
screen -S open_trader_dashboard_8766 -X quit 2>/dev/null || true
screen -dmS open_trader_dashboard_8766 zsh -lc \
  'cd /Users/ray/projects/open_trader && exec env PYTHONPATH=src .venv/bin/python -u -m open_trader dashboard --portfolio data/latest/portfolio.csv --data-dir data --reports-dir reports --poll-seconds 5 --host 127.0.0.1 --port 8766 >> /tmp/open_trader_dashboard_8766.log 2>&1'
make acceptance
```

Expected: final JSON status `PASS`. Fix any `FAIL`; report `BLOCKED` without substitutes.

---

### Task 2: 仅 A 股的动作优先 Dashboard

**Files:**
- Modify: `src/open_trader/dashboard.py:327-473`
- Test: `tests/test_dashboard.py:234-710`
- Modify: `src/open_trader/dashboard_static/dashboard.js:1870-1995`
- Modify: `src/open_trader/dashboard_static/dashboard.css:1416-1600,3695-3712`
- Test: `tests/test_dashboard_web.py:1731-1915`
- Modify: `src/open_trader/dashboard_acceptance.py:1-45,430-745,791-875`
- Test: `tests/test_dashboard_acceptance.py:401-1305`

**Interfaces:**
- Consumes: Task 1 frozen JSON additions and existing `_load_broker_trend_report(...)` report identity/action routing.
- Produces: unchanged `trend_reports` envelope plus complete CN `audit.candidates`, and DOM hooks `.cn-trend-report`, `.cn-trend-table`, `.cn-trend-card`, `.trend-discipline`, `.trend-audit` consumed by acceptance.

- [ ] **Step 1: Write the failing Dashboard projection test**

Add a CN payload whose `strategy_judgments.top10_candidates` contains only eligible rows while `signal_snapshots.candidates` contains eligible, excluded, and cash/slot-overflow candidates. Assert CN audit uses the complete signal ledger and other brokers still use the old top-ten list:

```python
def test_dashboard_projects_complete_cn_candidate_audit_only_for_eastmoney(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    write_csv(config.portfolio_path, PORTFOLIO_FIELDNAMES, [])
    payload = valid_trend_payload(market="CN", broker="eastmoney")
    payload["strategy_judgments"]["top10_candidates"] = [
        {"symbol": "688046", "strength": "99.9"}
    ]
    payload["signal_snapshots"] = {"candidates": [
        {"symbol": "688046", "eligible": True, "rank": 1,
         "excluded_reasons": [], "filter_price": "29.14"},
        {"symbol": "600000", "eligible": False, "rank": None,
         "excluded_reasons": ["strength_below_95"], "filter_price": "9.8"},
    ]}
    path = config.reports_dir / "trend_a_share/2026-07-15.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

    report = dashboard_module._load_trend_reports(
        config.data_dir, config.reports_dir, today=date(2026, 7, 15)
    )["eastmoney"]

    assert [item["symbol"] for item in report["audit"]["candidates"]] == [
        "688046", "600000"
    ]
```

- [ ] **Step 2: Run the projection test and verify RED**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_dashboard.py::test_dashboard_projects_complete_cn_candidate_audit_only_for_eastmoney
```

Expected: FAIL because the Dashboard currently projects only `top10_candidates`.

- [ ] **Step 3: Reuse the existing projection and select the complete CN ledger**

In `_load_broker_trend_report(...)`, validate `signal_snapshots.candidates` only when present and choose it only for `market == "CN"`; otherwise retain `judgments["top10_candidates"]`. Do not introduce a second report loader or change the response envelope.

- [ ] **Step 4: Write failing native-JS tests for the approved desktop and mobile structure**

Extend the existing `run_dashboard_js(...)` test payload with complete buy/sell/hold facts. Assert:

```javascript
const cn = renderTrendReportWorkspace({
  available:true,market:"CN",broker:"eastmoney",broker_label:"东方财富",
  market_label:"A股",report_date:"2026-07-16",data_date:"2026-07-15",
  generated_at:"2026-07-15T20:00:00+08:00",account_status:"已更新",
  buy_window:"09:30–10:00",counts:{sell:1,buy:1,hold:1,review:0},
  sell_actions:[{symbol:"601398",name:"工商银行",close:"7.2",
    temperature_prev:"温",temperature_curr:"温",strength:"91.3",
    reason:"left_trend_right_side",active_line:"7.0",
    entry_hints:["强度 91.3，低于入场线 95"]}],
  buy_actions:[{symbol:"688046",name:"药康生物",filter_price:"29.14",
    close:"28.81",temperature_prev:"温",temperature_curr:"热",phase:"立夏",
    strength:"99.9",industry:"医疗服务",industry_temperature:"热",
    market_cap:"110",amount:"6",target_weight:"0.04",
    target_amount:"27061.98",estimated_shares:900,
    estimated_initial_line:"24.55"}],
  hold_actions:[{symbol:"600900",name:"长江电力",close:"28.0",
    temperature_prev:"热",temperature_curr:"热",strength:"98.7",
    reason:"trend_intact",active_line:"27.8",entry_hints:["不是新的温转热或温转沸入场信号"]}],
  review_actions:[],audit:{candidates:[],excluded:{},industry_concentration:[],
    data_sources:["Trend Animals","Futu CN calendar/QFQ daily K-line"]},
});
for (const text of ["优先处理 · 卖出触发","09:30–10:00 · 正式买入计划",
  "盘中持续 · 已有持仓","筛选价","执行参考价","温 → 热","目标仓位 4%",
  "买入纪律","卖出纪律","审计详情"]) {
  if (!cn.includes(text)) throw new Error(cn);
}
if (!cn.includes('class="cn-trend-report"') ||
    !cn.includes('class="cn-trend-table"')) throw new Error(cn);

const us = renderTrendReportWorkspace({market:"US",broker_label:"富途",
  market_label:"美股",counts:{},sell_actions:[],buy_actions:[],
  hold_actions:[],review_actions:[],audit:{}});
if (us.includes('class="cn-trend-report"') || !us.includes("今日执行检查")) {
  throw new Error(us);
}
```

Add CSS assertions for existing color tokens, a `@media (max-width: 760px)` card transformation, `min-height: 44px`, and no fixed table width/min-width.

- [ ] **Step 5: Run JS/CSS tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_dashboard_web.py \
  -k 'trend_report_entries or trend_report_mobile or cn_trend'
```

Expected: FAIL because only the generic timeline renderer exists.

- [ ] **Step 6: Implement the CN renderer by reusing existing format/escape helpers**

Keep the current `renderTrendReportWorkspace(...)` body as `renderDefaultTrendReportWorkspace(...)`. Add one branch:

```javascript
function renderTrendReportWorkspace(report) {
  return String(report && report.market || "").toUpperCase() === "CN"
    ? renderCnTrendReportWorkspace(report)
    : renderDefaultTrendReportWorkspace(report);
}
```

Implement CN tables with semantic `<table>` markup and `data-label` on each `<td>`. Reuse `escapeHtml`, `formatPlain`, `TREND_REASON_LABELS`, current CSS variables, existing workspace open/close/focus behavior, and the existing `.trend-audit` disclosure. Use `window.matchMedia?.("(max-width: 760px)").matches` only to omit `open` from the two discipline `<details>` on mobile; default them open when `matchMedia` is unavailable so Node tests remain deterministic.

The main buy table must iterate only `report.buy_actions`; the audit iterates `audit.candidates`. Render `filter_price` as `筛选价（Trend Animals）`, `close` as `执行参考价（Futu 前复权）`, and convert decimal target weights with the existing numeric formatter rather than storing presentation-only percentages in JSON.

- [ ] **Step 7: Add minimal CSS using current Dashboard tokens**

Use the existing `--surface`, `--surface-soft`, `--line`, `--text`, `--muted`, `--accent`, danger and success variables. Desktop tables use `width: 100%`, `table-layout: fixed`, and normal wrapping. At `max-width: 760px`, hide `<thead>`, make each `<tr>` a bordered card, expose `td::before { content: attr(data-label) }`, and keep `overflow-x: hidden`. Do not add fonts, icons, gradients, or JavaScript layout measurements.

- [ ] **Step 8: Update acceptance checks before running the browser gate**

Add the new reason labels to both Python and JS maps. In `_check_account_holdings(...)`, preserve the generic checks for all brokers, then for eastmoney assert the three CN stage titles, both price-source labels, discipline disclosures, action text, and closed audit. Change the mobile viewport from 390 to 375 and assert:

```python
assert page.evaluate(
    "document.documentElement.scrollWidth <= window.innerWidth"
), "A 股趋势报告在 375px 产生横向滚动"
assert workspace.locator(".cn-trend-card:visible").count() >= 1
assert all(
    box is not None and box["x"] + box["width"] <= 376
    for box in workspace.locator(".cn-trend-card:visible").evaluate_all(
        "nodes => nodes.map(node => node.getBoundingClientRect()).map(r => ({x:r.x,width:r.width}))"
    )
)
```

Update fake locators in `tests/test_dashboard_acceptance.py` to expose the new classes and exact titles. Keep the real-browser check authoritative; do not substitute fixture screenshots.

- [ ] **Step 9: Run all Dashboard-focused tests**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_dashboard.py \
  tests/test_dashboard_web.py \
  tests/test_dashboard_acceptance.py
```

Expected: PASS.

- [ ] **Step 10: Commit, restart the exact SHA, and run the repository gate**

```bash
git add \
  src/open_trader/dashboard.py \
  src/open_trader/dashboard_static/dashboard.js \
  src/open_trader/dashboard_static/dashboard.css \
  src/open_trader/dashboard_acceptance.py \
  tests/test_dashboard.py \
  tests/test_dashboard_web.py \
  tests/test_dashboard_acceptance.py
git commit -m "feat: redesign A-share trend report dashboard"
screen -S open_trader_dashboard_8766 -X quit 2>/dev/null || true
screen -dmS open_trader_dashboard_8766 zsh -lc \
  'cd /Users/ray/projects/open_trader && exec env PYTHONPATH=src .venv/bin/python -u -m open_trader dashboard --portfolio data/latest/portfolio.csv --data-dir data --reports-dir reports --poll-seconds 5 --host 127.0.0.1 --port 8766 >> /tmp/open_trader_dashboard_8766.log 2>&1'
make acceptance
```

Expected: final JSON status `PASS` for real data, two refresh cycles, current process SHA, logs, desktop, and 375px mobile.

---

### Task 3: 一次性真实重生成、最终验收与部署

**Files:**
- Runtime overwrite: `reports/trend_a_share/2026-07-15.json`
- Runtime overwrite: `reports/trend_a_share/2026-07-15.md`
- Runtime update by the same existing workflow: `data/trend_a_share/protection_state.json`
- Runtime update by the same existing workflow: `data/trend_a_share/delivery/2026-07-15.json`
- Runtime update by the same existing workflow: `data/trend_a_share/daily_delivery/2026-07-15.json`
- No source file created or modified in this task.

**Interfaces:**
- Consumes: Task 1 private `_attempt_report(...)`, existing `RunLock`, `TrendAnimalsClient`, `FutuQuoteClient`, `_process_version(...)`, and `NullNotifier`.
- Produces: one base `2026-07-15` JSON/Markdown pair, matching receipt/protection state, no `-r1`, and no real notification attempt.

- [ ] **Step 1: Capture pre-run evidence and inspect live processes**

```bash
shasum -a 256 \
  reports/trend_a_share/2026-07-15.json \
  reports/trend_a_share/2026-07-15.md
stat -f '%Sm %N' -t '%Y-%m-%dT%H:%M:%S%z' \
  reports/trend_a_share/2026-07-15.json \
  reports/trend_a_share/2026-07-15.md
launchctl list | rg 'com.open-trader.trend-a-share-(report|watch)' || true
screen -ls
ps -axo pid,lstart,command | rg 'open_trader (dashboard|trend-a-share|watch-trend-a-share)'
```

Expected: identify every process that could retain pre-change code. Do not start the one-time run while another A-share report process is active.

- [ ] **Step 2: Run the existing private generation workflow exactly once with no notifier**

Run this from the repository root; it intentionally adds no CLI/product switch:

```bash
PYTHONPATH=src .venv/bin/python - <<'PY'
from pathlib import Path

from open_trader.a_share_trend import _attempt_report, _process_version
from open_trader.daily_premarket import RunLock, load_env_config
from open_trader.futu_quote import FutuQuoteClient
from open_trader.notifications import NullNotifier
from open_trader.trend_animals import TrendAnimalsClient

config = load_env_config(Path("config/daily_premarket.env"), dry_run=False)
with RunLock(config.data_dir / "runs/.trend_a_share_report.lock"):
    result = _attempt_report(
        config=config,
        run_date="2026-07-15",
        artifact_stem="2026-07-15",
        process_version=_process_version(config.repo),
        api_factory=TrendAnimalsClient,
        quote_factory=FutuQuoteClient,
        notifier=NullNotifier(),
    )
print(result)
assert result.status == "generated"
assert result.report_path == config.reports_dir / "trend_a_share/2026-07-15.md"
assert result.json_path == config.reports_dir / "trend_a_share/2026-07-15.json"
PY
```

Expected: one `generated` result; no `-r1` file and no Feishu/macOS notifier call. `_freeze_receipt_report` performs the existing temp-write, replace, rollback, and cleanup path.

- [ ] **Step 3: Validate the regenerated real artifact and no-notification state**

```bash
PYTHONPATH=src .venv/bin/python - <<'PY'
import json
from pathlib import Path

report = json.loads(Path("reports/trend_a_share/2026-07-15.json").read_text())
assert report["as_of_date"] == "2026-07-15"
assert report["execution_date"] == "2026-07-16"
assert report["metadata"]["market"] == "CN"
assert report["metadata"]["broker"] == "eastmoney"
assert report["delivery_status"] == "delivery_failed"
assert not list(Path("reports/trend_a_share").glob("2026-07-15-r*.json"))
assert not list(Path("reports/trend_a_share").glob("2026-07-15-r*.md"))

formal = report["strategy_judgments"]["formal_actions"]
buys = [item for item in formal if item["action"] == "BUY"]
sells = [item for item in formal if item["action"] == "SELL_ALL"]
holds = [
    item for item in report["strategy_judgments"]["holding_decisions"]
    if item["action"] == "HOLD"
]
assert [item["symbol"] for item in buys] == ["688046", "688796", "688222"]
assert len(sells) == 4
assert len(holds) == 1
assert all(item["filter_price"] and item["close"] for item in buys)
assert all(item["target_weight"] in {"0.04", "0.02"} for item in buys)
assert all("industry_temperature" in item for item in buys)
print({"buy": len(buys), "sell": len(sells), "hold": len(holds)})
PY

shasum -a 256 \
  reports/trend_a_share/2026-07-15.json \
  reports/trend_a_share/2026-07-15.md
stat -f '%Sm %N' -t '%Y-%m-%dT%H:%M:%S%z' \
  reports/trend_a_share/2026-07-15.json \
  reports/trend_a_share/2026-07-15.md
```

Expected: new hashes/timestamps, `3` formal buys, `4` sells, `1` hold, both prices present, and no revision files. If live immutable source data produces a different action count, stop and reconcile the facts instead of weakening the assertion.

- [ ] **Step 4: Restart every long-running process that can retain old code**

Inspect `launchctl print gui/$(id -u)/com.open-trader.trend-a-share-watch` and the Dashboard screen. Kickstart only installed A-share jobs that are meant to be running; always replace the Dashboard listener with one process from the accepted working tree. Record old and new PIDs.

- [ ] **Step 5: Run the final repository acceptance gate**

```bash
make acceptance
```

Expected: output JSON has `status == "PASS"`, `errors == []`, `blocker is None`, and a non-empty `pid`. On `FAIL`, diagnose and fix before rerunning. On `BLOCKED`, report the browser/external blocker and do not substitute curl, mocks, screenshots, or unit tests.

- [ ] **Step 6: Redeploy the exact accepted SHA without source/data changes**

```bash
ACCEPTED_SHA=$(git rev-parse HEAD)
OLD_PID=$(lsof -nP -iTCP:8766 -sTCP:LISTEN -t)
LOG_SIZE=$(stat -f '%z' /tmp/open_trader_dashboard_8766.log 2>/dev/null || echo 0)
screen -S open_trader_dashboard_8766 -X quit 2>/dev/null || true
kill -TERM "$OLD_PID" 2>/dev/null || true
screen -dmS open_trader_dashboard_8766 zsh -lc \
  'cd /Users/ray/projects/open_trader && exec env PYTHONPATH=src .venv/bin/python -u -m open_trader dashboard --portfolio data/latest/portfolio.csv --data-dir data --reports-dir reports --poll-seconds 5 --host 127.0.0.1 --port 8766 >> /tmp/open_trader_dashboard_8766.log 2>&1'
```

Poll for a new listener rather than using a long blind sleep.

- [ ] **Step 7: Verify post-acceptance deployment and hand off the URL**

```bash
NEW_PID=$(lsof -nP -iTCP:8766 -sTCP:LISTEN -t)
test "$NEW_PID" != "$OLD_PID"
ps -p "$NEW_PID" -o pid=,ppid=,lstart=,command=
lsof -a -p "$NEW_PID" -d cwd -Fn
test "$(git rev-parse HEAD)" = "$ACCEPTED_SHA"
tail -c +$((LOG_SIZE + 1)) /tmp/open_trader_dashboard_8766.log
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/
```

Expected: a new PID, cwd `/Users/ray/projects/open_trader`, exact `ACCEPTED_SHA`, fresh startup logs without traceback, and HTTP `200`. Provide `http://127.0.0.1:8766/` only after all checks pass.
