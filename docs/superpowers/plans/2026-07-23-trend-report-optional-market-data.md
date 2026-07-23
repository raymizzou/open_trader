# Trend Report Optional Market Data Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 A 股、美股和港股趋势报告在行情、ATR14 或港股每手股数暂时缺失时仍生成正式买入动作，并在数据恢复后安全执行和补建保护线。

**Architecture:** 保留 Trend Animals 作为候选资格的唯一来源，把 Futu 行情、ATR14 和港股每手股数改为 v5 动作的可延迟补全字段。复用现有不可变报告、动作账本、修订请求、通知去重和保护状态；v1-v4 校验保持冻结，v5 在执行时用实时价和实时每手股数完成数量，并把无 ATR 成交记录为待补全保护状态。修订报告使用同日递增批次，只有显式、模拟盘限定的授权可以越过正常买入窗口。

**Tech Stack:** Python 3.12、标准库 `dataclasses`/`decimal`/`pathlib`、pytest、现有 Trend Animals/Futu 客户端、原生 JavaScript、现有 Feishu 通知与 Dashboard acceptance。

## Global Constraints

- 隔离工作树：`/Users/ray/projects/open_trader/.worktrees/trend-report-optional-market-data`，分支 `fix/trend-report-optional-market-data`。
- 已合入本地 `main` `e06e264`；合并后基线为 `3166 passed in 73.22s`，命令必须使用 `.venv/bin/python -m pytest`。
- 新策略身份固定为 `trend_animals_warm_to_hot/{market}/v5`，生效日期 `2026-07-23`；v1-v4 报告和校验不可改变。
- Trend Animals 资格和排序不依赖 Futu 行情、ATR14 或港股每手股数。
- 缺少实时价、合法交易单位、有效净值或现金时不得猜测数据或提交订单。
- 每个市场/账户持仓上限维持 10 只；已知组合计划风险达到净值 4% 时仍暂停新买入。
- 缺 ATR 的动作不等待、不降级、不增加数量上限；正常目标仓位、Kelly、现金、席位和交易单位仍生效。
- `2026-07-22-r1` 是唯一获批的窗口外模拟盘执行例外；默认调度仍严格遵守窗口。
- 不增加依赖、不新增行情源、不使用 Trend Animals `priceIndex` 冒充实时价。
- 开发中只运行聚焦测试和直接工作流；`make acceptance` 只在最终交付门禁运行一次。

---

### Task 1: v5 候选、动作与风险契约

**Files:**
- Modify: `src/open_trader/a_share_trend.py`
- Modify: `src/open_trader/trend_review.py`
- Test: `tests/test_a_share_trend.py`
- Test: `tests/test_trend_review.py`

**Interfaces:**
- Produces: `valid_v5_risk_contract(parameters, summary, *, expected_nav) -> bool`
- Produces: `build_candidate_list(rows, *, held_symbols, expected_date=None, market="CN", require_atr=True) -> CandidateDecision`；只有 v5 报告传 `False`
- Produces: v5 `BuyAction`，其中 `close`、`atr`、`lot_size`、`estimated_shares`、`estimated_initial_line`、`planned_stop_risk`、`planned_stop_risk_pct`、`normal_cost` 可为 `None`
- Produces: `BuyAction.market_data_status: str` 和 `BuyAction.pending_fields: tuple[str, ...]`
- Produces: `_post_sell_planned_risk -> tuple[Decimal, tuple[str, ...], str]`，依次返回已知风险、未知风险标的、真正的硬阻塞原因

- [ ] **Step 1: 把 ATR 排除断言改成 v5 可进入断言**

在 `tests/test_a_share_trend.py` 增加 v5 回撤测试事实：

```python
def active_drawdown_summary(
    snapshot: Mapping[str, object],
    execution_date: str,
    equity: str = "100000",
) -> dict[str, object]:
    market = str(snapshot["market"])
    strategy_id = str(snapshot["strategy_id"])
    version = str(snapshot["strategy_version"])
    return {
        "schema_version": "open_trader.strategy_drawdown.v1",
        "market": market,
        "strategy_id": strategy_id,
        "strategy_version": version,
        "kelly_sample_key": f"{market}|{strategy_id}|{version}",
        "state_status": "ok",
        "status": "active",
        "status_label": "纪律内",
        "entry_allowed": True,
        "current_equity": equity,
        "high_water_mark": equity,
        "drawdown_pct": "0",
        "drawdown_limit_pct": "0.05",
        "pause_reason": "",
        "paused_at": None,
        "observed_at": f"{execution_date}T08:00:00+08:00",
        "bootstrap_event": None,
        "recovery_event": None,
    }
```

将 `test_stale_candidate_kline_is_unavailable_and_excluded`、`test_candidate_kline_failure_is_an_atr_exclusion` 和 `test_invalid_candidate_kline_is_an_atr_exclusion` 改成：

```python
assert item.close is None
assert item.atr is None
decision = build_candidate_list(
    [item],
    held_symbols=set(),
    expected_date=item.as_of_date,
    require_atr=False,
)
assert decision.eligible == (item,)
assert item.symbol not in decision.excluded
```

新增混合候选和 v5 动作测试：

```python
def test_v5_missing_quote_and_atr_keeps_formal_buy_without_pausing_batch() -> None:
    missing = replace(candidate("600001"), close=None, atr=None)
    complete = candidate("600002")
    snapshot = trend_module.live_trend_strategy_snapshot(
        "CN", "v5sha", (622466, 697199)
    )
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account(),
        candidates=[missing, complete],
        holding_snapshots={},
        bars_by_symbol={},
        strategy_snapshot=snapshot,
        drawdown_summary=active_drawdown_summary(snapshot, "2026-07-15"),
    )

    assert [item.symbol for item in built.buy_actions] == ["600001", "600002"]
    pending = built.buy_actions[0]
    assert pending.market_data_status == "pending"
    assert pending.pending_fields == ("quote", "atr")
    assert pending.estimated_shares is None
    assert pending.planned_stop_risk is None
    assert built.risk_summary["status"] == "active_with_unknown_risk"
    assert built.risk_summary["unknown_new_risk_symbols"] == ["600001"]
```

新增港股每手股数和已有持仓未知风险测试：

```python
def test_v5_hk_missing_lot_size_keeps_pending_formal_buy() -> None:
    item = replace(candidate("600001"), symbol="00700", exchange="HK")
    snapshot = trend_module.live_trend_strategy_snapshot(
        "HK", "v5sha", (622460,)
    )
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account(),
        candidates=[item],
        holding_snapshots={},
        bars_by_symbol={},
        market="HK",
        lot_sizes={},
        strategy_snapshot=snapshot,
        drawdown_summary=active_drawdown_summary(snapshot, "2026-07-15"),
    )
    action = built.buy_actions[0]
    assert action.lot_size is None
    assert action.pending_fields == ("lot_size",)


def test_v5_existing_unknown_risk_does_not_pause_known_buys() -> None:
    snapshot = trend_module.live_trend_strategy_snapshot(
        "CN", "v5sha", (622466, 697199)
    )
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account("600001"),
        candidates=[candidate("600002")],
        holding_snapshots={"600001": holding("600001")},
        bars_by_symbol={},
        prior_state={"schema_version": 1, "positions": {}},
        strategy_snapshot=snapshot,
        drawdown_summary=active_drawdown_summary(snapshot, "2026-07-15"),
    )
    assert [item.symbol for item in built.buy_actions] == ["600002"]
    assert built.risk_summary["unknown_existing_risk_symbols"] == ["600001"]
    assert built.risk_summary["status"] == "active_with_unknown_risk"
```

- [ ] **Step 2: 运行测试并确认旧逻辑失败**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_a_share_trend.py::test_v5_missing_quote_and_atr_keeps_formal_buy_without_pausing_batch \
  tests/test_a_share_trend.py::test_v5_hk_missing_lot_size_keeps_pending_formal_buy \
  tests/test_a_share_trend.py::test_v5_existing_unknown_risk_does_not_pause_known_buys -q
```

Expected: FAIL；第一个缺数据候选没有正式动作，港股缺每手股数被跳过，已有持仓缺保护线导致整批暂停。

- [ ] **Step 3: 实现最小 v5 数据契约**

在 `live_trend_strategy_snapshot` 中把身份升级为 v5，并只改硬门槛描述：

```python
parameters.update({
    "requires_atr14": False,
    "market_data_completion": "deferred_to_execution",
})
return {
    **snapshot,
    "strategy_id": f"trend_animals_warm_to_hot/{market}/v5",
    "strategy_version": "v5",
    "effective_from": "2026-07-23",
    "parameters": parameters,
    "parameter_rows": [
        *snapshot["parameter_rows"],
        {
            "group": "累计回撤",
            "name": "策略累计回撤暂停",
            "value": "纪律模拟策略净值从高点回撤达到 5% 时暂停新开仓，人工解锁后重设基准",
        },
    ],
}
```

给 `_candidate_reasons` 和 `build_candidate_list` 增加 `require_atr=True`，把旧判断改为：

```python
if require_atr and item.atr is None:
    reasons.append("atr_unavailable")
```

`build_report` 调用候选列表时传 `require_atr=snapshot_version != "v5"`，因此 v1-v4 冻结重放仍保持旧资格契约。

把 `BuyAction` 的可延迟字段改成可空，并添加：

```python
market_data_status: str
pending_fields: tuple[str, ...]
```

在 `_plan_buy_actions` 中保留净值、现金、汇率、Kelly、回撤和已知 4% 风险硬门禁；删除 `portfolio_planned_risk is None` 和候选 `close/atr` 的整批暂停。每个候选计算：

```python
lot_size = (
    100 if market == "CN"
    else (lot_sizes or {}).get(item.symbol) if market == "HK"
    else 1
)
pending_fields = tuple(
    name for name, missing in (
        ("quote", item.close is None),
        ("atr", item.atr is None),
        ("lot_size", lot_size is None or lot_size <= 0),
    )
    if missing
)
```

完整数据继续调用 `size_entry_by_risk`。数据不完整时生成正式动作，目标金额为 `min(net_value * weight, remaining_cash)`；只有 `close` 和 `lot_size` 可用时估算股数并扣减计划现金。缺报价或每手股数的动作占用报告席位但不预留现金；不递补报告外候选。

把 `_post_sell_planned_risk` 改为累计可计算持仓风险，并把缺行情/保护线的标的放入 `unknown_existing_risk_symbols`；无效净值、现金、汇率、成本或持仓数量仍返回硬阻塞原因。

新增 `valid_v5_risk_contract`：复用 v4 的固定参数、Kelly 和回撤字段校验，但允许 `active_with_unknown_risk`，要求未知列表为去重后的非空字符串列表，并要求任何未知风险存在时汇总风险字段为 `None`、`new_known_planned_risk` 为非负数。

在 `validate_report_strategy_snapshot` 中分开 v1-v4 严格路径和 v5 可空路径；v5 完整动作仍校验 `close - 2 * atr`、单笔风险和组合风险，待补全动作校验 `pending_fields` 与空字段一致。

在 `rebuild_trend_report_from_evidence` 的 Kelly、成本和回撤版本集合中加入 v5；v5 仍要求冻结 `normal_cost_rate`、`kelly_rounds`、`kelly_data_reason` 和 `drawdown_summary`，重放时不得重新请求行情。新增测试冻结一个 `close=None`、`atr=None` 的 v5 候选并断言重放后的 `pending_fields` 与原报告一致。

- [ ] **Step 4: 运行聚焦回归**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_a_share_trend.py \
  tests/test_trend_review.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add \
  src/open_trader/a_share_trend.py \
  src/open_trader/trend_review.py \
  tests/test_a_share_trend.py \
  tests/test_trend_review.py
git commit -m "feat: defer trend entry market data"
```

---

### Task 2: JSON、Markdown、Feishu 与 Dashboard 待补全展示

**Files:**
- Modify: `src/open_trader/a_share_trend.py`
- Modify: `src/open_trader/dashboard.py`
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Test: `tests/test_a_share_trend.py`
- Test: `tests/test_dashboard.py`
- Test: `tests/test_dashboard_web.py`

**Interfaces:**
- Consumes: Task 1 的 v5 `BuyAction.market_data_status` 和 `pending_fields`
- Produces: 四种输出统一使用“待补全”，不把空行情、股数、ATR、风险或保护线显示为 `0`

- [ ] **Step 1: 写失败的输出测试**

在 `tests/test_a_share_trend.py` 新增：

```python
def test_pending_v5_buy_renders_pending_instead_of_zero() -> None:
    snapshot = trend_module.live_trend_strategy_snapshot(
        "CN", "v5sha", (622466, 697199)
    )
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account(),
        candidates=[replace(candidate("600001"), close=None, atr=None)],
        holding_snapshots={},
        bars_by_symbol={},
        strategy_snapshot=snapshot,
        drawdown_summary=active_drawdown_summary(snapshot, "2026-07-15"),
    )
    payload = trend_module._report_payload(built)
    markdown = render_markdown(built)
    feishu = render_trend_feishu_text(payload)

    action = payload["strategy_judgments"]["formal_actions"][0]
    assert action["estimated_shares"] is None
    assert action["estimated_initial_line"] is None
    assert "待行情、ATR 补全" in markdown
    assert "约 0 股" not in markdown
    assert "保护线 0" not in feishu
    assert "正式动作仍有效" in feishu
```

在 `tests/test_dashboard.py` 添加 v5 可空动作投影断言，在 `tests/test_dashboard_web.py` 添加：

```javascript
const html = renderCnBuyStage({buy_actions:[{
  symbol:"600001", name:"示例", target_amount:"4000",
  estimated_shares:null, estimated_initial_line:null,
  market_data_status:"pending", pending_fields:["quote","atr"]
}]});
assert(html.includes("待补全"));
assert(!html.includes(">0 股<"));
```

- [ ] **Step 2: 运行测试并确认失败**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_a_share_trend.py -k pending_v5_buy_renders \
  tests/test_dashboard.py -k pending_market_data \
  tests/test_dashboard_web.py -k pending_market_data -q
```

Expected: FAIL；当前 Feishu 使用 `or '0'`，Dashboard 风险校验拒绝 v5 可空动作。

- [ ] **Step 3: 最小化修改渲染与校验**

在 `render_trend_feishu_text` 和 `render_markdown` 中用一个本地展示规则：

```python
shares = f"{item.estimated_shares} 股" if item.estimated_shares else "股数待补全"
line = (
    _money(item.estimated_initial_line)
    if item.estimated_initial_line is not None
    else "待补全"
)
pending = "、".join({
    "quote": "行情", "atr": "ATR", "lot_size": "每手股数"
}[field] for field in item.pending_fields)
```

Feishu 正式动作追加 `正式动作仍有效｜{pending}待补全`。风险汇总为未知时显示“已知风险 + 未知标的”，不调用百分比格式化处理 `None`。

在 `dashboard.py` 增加 v5 到版本分派，并让 `_valid_v2_risk_items` 的 v5 分支验证 `market_data_status`、`pending_fields` 和可空字段；v1-v4 分支不放宽。

在 `dashboard.js` 复用现有 `hasValue`，缺失字段显示 `待补全`，并在买入卡片增加一个现有样式的状态文本；不新增组件或依赖。

- [ ] **Step 4: 运行输出与浏览器单测**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_a_share_trend.py \
  tests/test_dashboard.py \
  tests/test_dashboard_web.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add \
  src/open_trader/a_share_trend.py \
  src/open_trader/dashboard.py \
  src/open_trader/dashboard_static/dashboard.js \
  tests/test_a_share_trend.py \
  tests/test_dashboard.py \
  tests/test_dashboard_web.py
git commit -m "feat: show pending trend execution data"
```

---

### Task 3: 实时补量、无 ATR 成交与保护线恢复

**Files:**
- Modify: `src/open_trader/trend_review.py`
- Modify: `src/open_trader/trend_market_controller.py`
- Modify: `src/open_trader/a_share_trend.py`
- Modify: `src/open_trader/notification_policy.py`
- Test: `tests/test_trend_review.py`
- Test: `tests/test_trend_market_controller.py`
- Test: `tests/test_a_share_trend.py`
- Test: `tests/test_notification_policy.py`

**Interfaces:**
- Produces: `execute_trend_review_open` 新参数 `quote_lot_sizes=()`、`allow_late_buys=False`
- Produces: `_remaining_buy_quantity` 新参数 `live_lot_size=None`、`available_cash=None`，返回 `int`
- Produces: 保护状态字段 `entry_fill_price`、`protection_status`、`protection_pending_since`
- Consumes: Task 1 的 v5 可空动作

- [ ] **Step 1: 写执行与保护失败测试**

在 `tests/test_trend_review.py` 先增加确定性 helper：

```python
def v5_report_with_pending_buy(
    *,
    market: str = "CN",
    symbol: str = "600001",
    target_amount: str = "4000",
    lot_size: int | None = 100,
) -> dict[str, object]:
    payload = cn_buy_report(symbol=symbol)
    payload["strategy_snapshot"] = {
        "strategy_id": f"trend_animals_warm_to_hot/{market}/v5",
        "strategy_version": "v5",
        "process_version": "v5sha",
        "parameters": {"buy_window": "09:30-10:00"},
        "parameter_rows": [
            {"group": "仓位执行", "name": "买入窗口", "value": "09:30-10:00"}
        ],
    }
    payload["strategy_judgments"] = {"formal_actions": [{
        "action": "BUY",
        "symbol": symbol,
        "target_weight": "0.04",
        "target_amount": target_amount,
        "lot_size": lot_size,
        "estimated_shares": None,
        "atr": None,
        "estimated_initial_line": None,
        "planned_stop_risk": None,
        "planned_stop_risk_pct": None,
        "normal_cost": None,
        "market_data_status": "pending",
        "pending_fields": [
            field for field, missing in (
                ("quote", True), ("atr", True), ("lot_size", lot_size is None)
            )
            if missing
        ],
    }]}
    return payload
```

在 `tests/test_trend_review.py` 新增：

```python
def test_v5_buy_uses_live_quote_without_atr_or_estimate(tmp_path: Path) -> None:
    client = FakeTrendSimClient()
    report = v5_report_with_pending_buy(target_amount="4000", lot_size=100)
    result = trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=report,
        client=client,
        market="CN",
        execution_date="2026-07-23",
        now="2026-07-23T09:31:00+08:00",
        quote_prices={"SH.600001": Decimal("10.20")},
    )
    assert result["submitted_count"] == 1
    assert client.requests[0]["qty"] == "300"


def test_v5_hk_buy_uses_live_lot_size(tmp_path: Path) -> None:
    client = FakeTrendSimClient()
    report = v5_report_with_pending_buy(
        market="HK", symbol="00700", target_amount="52000", lot_size=None
    )
    trend_review.execute_trend_review_open(
        data_dir=tmp_path,
        report=report,
        client=client,
        market="HK",
        execution_date="2026-07-23",
        now="2026-07-23T09:31:00+08:00",
        quote_prices={"HK.00700": Decimal("510")},
        quote_lot_sizes={"HK.00700": 100},
    )
    assert client.requests[0]["qty"] == "100"
```

新增成交后保护状态测试：

```python
def test_v5_fill_without_atr_persists_pending_protection(tmp_path: Path) -> None:
    client = FakeTrendSimClient()
    arguments = {
        "data_dir": tmp_path,
        "report": v5_report_with_pending_buy(target_amount="4000", lot_size=100),
        "client": client,
        "market": "CN",
        "execution_date": "2026-07-23",
        "now": "2026-07-23T09:31:00+08:00",
        "quote_prices": {"SH.600001": Decimal("10.2")},
    }
    trend_review.execute_trend_review_open(**arguments)
    request = client.requests[0]
    client.orders = [{
        "order_id": "SIM-1",
        "remark": request["remark"],
        "code": "SH.600001",
        "trd_side": "BUY",
        "qty": request["qty"],
        "dealt_qty": request["qty"],
        "dealt_avg_price": "10.2",
        "order_status": "FILLED_ALL",
    }]
    trend_review.execute_trend_review_open(**arguments)
    state = json.loads(
        (tmp_path / "trend_a_share/protection_state.json").read_text()
    )["positions"]["600001"]
    assert state["entry_fill_price"] == "10.2"
    assert state["protection_status"] == "pending"
    assert "initial_line" not in state
    action_key = trend_review.trend_action_key(
        "CN", "2026-07-23", "SH.600001", "buy"
    )
    events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (
            tmp_path / "trend_review/ledgers/CN/actions/2026-07-23" / action_key
        ).glob("*.json")
    ]
    assert any(event.get("protection_status") == "pending" for event in events)
```

测试保护恢复：

```python
def test_pending_protection_recovers_from_fill_price_and_entry_day_atr() -> None:
    daily_bars = bars()
    entry_atr = atr14(daily_bars)
    assert entry_atr is not None
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account("600001"),
        candidates=[],
        holding_snapshots={"600001": holding("600001")},
        prior_state={"schema_version": 1, "positions": {"600001": {
            "entry_fill_price": "10.2",
            "protection_status": "pending",
            "protection_pending_since": "2026-07-14T10:35:00+08:00",
            "position_started_for": "2026-07-14",
            "updated_for": "2026-07-14",
        }}},
        bars_by_symbol={"600001": daily_bars},
    )
    state = built.protection_state["positions"]["600001"]
    assert Decimal(state["initial_line"]) == Decimal("10.2") - Decimal("2") * entry_atr
    assert state["protection_status"] == "active"
    assert state["protection_recovered_for"] == built.as_of_date
```

- [ ] **Step 2: 运行测试并确认失败**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_trend_review.py -k "v5_buy_uses_live or v5_fill_without_atr" \
  tests/test_a_share_trend.py -k pending_protection_recovers -q
```

Expected: FAIL；v4 前置校验要求 ATR/预计股数，成交保护函数也要求 ATR。

- [ ] **Step 3: 实现 v5 实时补量**

在 `_preflight_open_actions` 中保持 v1-v4 严格校验；v5 要求正数 `target_weight` 和 `target_amount`，允许 Task 1 定义的可空字段，并验证 `pending_fields`。

在 `_remaining_buy_quantity` 中：

```python
lot_size = live_lot_size or int(action.get("lot_size") or 0)
caps = [
    _floor_to_lot(remaining_amount / (current_price * fx), lot_size),
    _floor_to_lot(cash / (current_price * fx), lot_size),
]
if version in {"v1", "v2", "v3", "v4"}:
    caps.insert(0, _floor_to_lot(remaining_quantity, lot_size))
if (
    version == "v5"
    and action.get("atr") is not None
    and action.get("planned_stop_risk") is not None
):
    atr = _required_decimal(action["atr"], "action ATR")
    planned_risk = _required_decimal(
        action["planned_stop_risk"], "planned stop risk"
    )
    cost_rate = _required_decimal(
        risk_summary["normal_cost_rate"], "normal cost rate"
    )
    confirmed_risk = sum(
        quantity * (Decimal("2") * atr * fx + price * fx * cost_rate)
        for quantity, price in fills.values()
    )
    unit_risk = Decimal("2") * atr * fx + current_price * fx * cost_rate
    caps.append(
        _floor_to_lot((planned_risk - confirmed_risk) / unit_risk, lot_size)
    )
```

不要用报告预计股数限制 v5；实时价变动只改变股数，不改变目标金额。`execute_trend_review_open` 在同一轮为后续动作扣减已提交订单的估算占用现金，防止多个待补全动作共用同一份快照现金。

在 `_execute_locked_report` 保留完整 snapshot 对象：

```python
snapshots = quote.get_snapshots(symbols)
prices = {symbol: item.last_price for symbol, item in snapshots.items()}
lot_sizes = {symbol: item.lot_size for symbol, item in snapshots.items()}
```

将 `lot_sizes` 传给执行器；单个缺报价或每手股数只写该动作 `pending` 事件，其他动作继续。

- [ ] **Step 4: 保存无 ATR 成交并恢复保护**

把 `_activate_fill_protection_line` 收窄改为 `_record_fill_protection`，始终接收 `average_price`，ATR 可空：

```python
position = {
    **prior,
    "entry_fill_price": format(average_price, "f"),
    "position_started_for": str(prior.get("position_started_for") or execution_date),
    "updated_for": execution_date,
}
if atr is None:
    position.update({
        "protection_status": "pending",
        "protection_pending_since": recorded_at,
    })
else:
    line = average_price - Decimal("2") * atr
    position.update({
        "initial_line": str(prior.get("initial_line") or line),
        "active_line": format(line, "f"),
        "atr14": format(atr, "f"),
        "protection_status": "active",
    })
```

成交事件保留 `status="filled"`，另加 `protection_status="pending"`，这样既不破坏完成判定，也能复用动作通知。

在 `build_report` 中，只有 `active_line is None` 且存在 `entry_fill_price` 时执行补全：优先以 `position_started_for` 截止的 K 线计算 ATR14，确实不足 14 根时使用当前首次可计算 ATR14；保护线基于 `entry_fill_price`，写 `protection_recovered_for`，随后仍走现有只升不降逻辑。历史状态没有 `entry_fill_price` 时保留原兼容行为。

在 `_validate_protection_state` 仅增加上述字段的日期、时区和正数校验。

- [ ] **Step 5: 复用通知去重发送状态变化**

在 `notification_policy.py` 增加：

```python
PROTECTION_STATUS_LABELS = {
    "pending": ("已成交但保护线待补全", "每日趋势报告继续监控，等待 ATR 恢复"),
    "active": ("保护线已恢复", "按新保护线继续自动监控"),
}
```

控制器只对成交事件首次出现 `protection_status="pending"` 调用现有 `_notify_feishu_once`；恢复状态由当日趋势报告显示 `protection_recovered_for`。去重 key 固定为 `(market, execution_date, symbol, protection_status)`，不使用每轮时间制造重复。

- [ ] **Step 6: 运行聚焦回归**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_trend_review.py \
  tests/test_trend_market_controller.py \
  tests/test_a_share_trend.py \
  tests/test_notification_policy.py -q
```

Expected: PASS。

- [ ] **Step 7: 提交**

```bash
git add \
  src/open_trader/trend_review.py \
  src/open_trader/trend_market_controller.py \
  src/open_trader/a_share_trend.py \
  src/open_trader/notification_policy.py \
  tests/test_trend_review.py \
  tests/test_trend_market_controller.py \
  tests/test_a_share_trend.py \
  tests/test_notification_policy.py
git commit -m "feat: execute and protect deferred trend buys"
```

---

### Task 4: 修订报告批次与一次性窗口外模拟执行

**Files:**
- Modify: `src/open_trader/trend_review.py`
- Modify: `src/open_trader/trend_market_controller.py`
- Modify: `src/open_trader/cli.py`
- Modify: `src/open_trader/dashboard.py`
- Modify: `CHANGELOG.md`
- Test: `tests/test_trend_review.py`
- Test: `tests/test_trend_market_controller.py`
- Test: `tests/test_dashboard.py`
- Test: `tests/test_trend_market_cli.py`

**Interfaces:**
- Produces: `lock_trend_execution_batch` 新参数 `revision: int = 0`
- Produces: `run_corrected_trend_report(config, market, *, actor, reason, allow_late_buys=False, now_fn=datetime.now) -> dict[str, object]`
- Produces CLI: `open-trader trend-market correct --market CN --actor <name> --reason <text> [--allow-late-buys]`

- [ ] **Step 1: 写修订批次和授权失败测试**

保留现有 `test_later_revision_does_not_change_locked_batch`，证明未授权修订仍不能改变已锁批次。新增：

```python
def test_completed_in_window_correction_gets_revision_batch_and_executes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    cycle = active_cn_cycle()
    base_path, base = write_report(config)
    controller._request_revision(config, cycle, NOW)
    lock_trend_execution_batch(
        config.data_dir, market="CN", execution_date=cycle.execution_date,
        report_path=base_path, report=base, locked_at=NOW.isoformat()
    )
    revision_path, revision = write_report(config, revision=1, buy=True)
    monkeypatch.setattr(
        controller, "_recovery_revision_for_report",
        lambda *_args, **_kwargs: None,
    )
    controller._complete_revision(
        config, cycle, (revision_path, revision), NOW
    )
    selected = controller._locked_report(
        config, cycle, (revision_path, revision), NOW
    )
    assert selected[0] == revision_path
    lock_trend_execution_batch(
        config.data_dir,
        market="CN",
        execution_date=cycle.execution_date,
        report_path=selected[0],
        report=selected[1],
        locked_at=NOW.isoformat(),
        revision=1,
    )
    assert controller._batch_path(
        config, "CN", cycle.execution_date, revision=1
    ).exists()
```

新增默认拒绝和一次性模拟盘授权：

```python
def test_corrected_report_rejects_late_buy_without_explicit_simulate_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    cycle = replace(
        active_cn_cycle(),
        as_of_date="2026-07-22",
        execution_date="2026-07-23",
        report_run_date="2026-07-22",
    )
    monkeypatch.setattr(controller, "_derive_cycle", lambda *_args, **_kwargs: cycle)
    monkeypatch.setattr(
        controller, "_cycle_to_reconcile", lambda *_args, **_kwargs: cycle
    )
    monkeypatch.setattr(controller.socket, "gethostname", lambda: "executor")
    with pytest.raises(ValueError, match="outside buy window"):
        run_corrected_trend_report(
            config, "CN",
            actor="ray", reason="ATR exclusion bug",
            allow_late_buys=False,
            now_fn=lambda: datetime.fromisoformat("2026-07-23T10:30:00+08:00"),
        )


def test_corrected_report_late_authorization_is_hash_bound_and_one_shot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = controller_config(tmp_path)
    revision_path = (
        config.reports_dir / "trend_a_share/2026-07-22-r1.json"
    )
    revision_path.parent.mkdir(parents=True, exist_ok=True)
    revision = valid_cn_report(
        as_of_date="2026-07-22", execution_date="2026-07-23", buy=True
    )
    revision["generated_at"] = "2026-07-23T10:30:00+08:00"
    revision_path.write_text(json.dumps(revision), encoding="utf-8")
    cycle = replace(
        active_cn_cycle(),
        as_of_date="2026-07-22",
        execution_date="2026-07-23",
        report_run_date="2026-07-22",
    )
    now = datetime.fromisoformat("2026-07-23T10:30:00+08:00")
    monkeypatch.setattr(controller, "_derive_cycle", lambda *_args, **_kwargs: cycle)
    monkeypatch.setattr(
        controller, "_cycle_to_reconcile", lambda *_args, **_kwargs: cycle
    )
    monkeypatch.setattr(controller.socket, "gethostname", lambda: "executor")
    monkeypatch.setattr(controller, "_request_revision", lambda *_args: None)
    monkeypatch.setattr(controller, "_generate_report", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        controller,
        "_pending_revision_report",
        lambda *_args, **_kwargs: (revision_path, revision),
    )
    monkeypatch.setattr(controller, "_complete_revision", lambda *_args: None)
    monkeypatch.setattr(
        controller,
        "_execute_locked_report",
        lambda *_args, **_kwargs: {
            "status": "submitted", "submitted_count": 1
        },
    )
    result = run_corrected_trend_report(
        config, "CN",
        actor="ray", reason="ATR exclusion bug",
        allow_late_buys=True,
        now_fn=lambda: now,
    )
    authorization_path = (
        config.data_dir
        / "trend_controller/CN/late_buy_authorizations/2026-07-23.json"
    )
    authorization = json.loads(authorization_path.read_text(encoding="utf-8"))
    assert authorization["report_path"].endswith("2026-07-22-r1.json")
    assert authorization["report_sha256"] == result["report_sha256"]
    assert authorization["actor"] == "ray"
    assert authorization["reason"] == "ATR exclusion bug"
    assert result["execution_date"] == "2026-07-23"
```

- [ ] **Step 2: 运行测试并确认失败**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_trend_market_controller.py -k "correction or later_revision" \
  tests/test_trend_review.py -k revision_batch -q
```

Expected: FAIL；当前批次路径每天唯一，且没有显式窗口外授权入口。

- [ ] **Step 3: 版本化不可变执行批次**

给 `lock_trend_execution_batch` 增加 `revision=0`，路径规则：

```python
suffix = "" if revision == 0 else f"-r{revision}"
path = batches_root / f"{execution_date}{suffix}.json"
```

批次 payload 增加整数 `report_revision`。`_batch_path` 同样接受 `revision=0`。`_locked_report` 只有在修订请求已经完成且最新报告哈希等于 completion 哈希时才选择相同 revision 的批次；普通晚到报告继续返回原批次，保持现有测试。

`_execution_completed`、Dashboard 批次投影和控制器恢复选择最新已完成修订对应的批次；旧批次保持不可变。动作 key 和 Futu remark 继续使用既有 `market/date/symbol/side`，因此重启或重复命令不会重复下单。

- [ ] **Step 4: 实现同步修订命令与窗口授权**

`run_corrected_trend_report` 顺序固定：

```python
cycle = _cycle_to_reconcile(config, _derive_cycle(config, market, now), now)
_request_revision(config, cycle, now)
_generate_report(config, market, cycle.report_run_date, revision=True)
report = _pending_revision_report(config, cycle, request)
_complete_revision(config, cycle, report, now)
```

窗口内直接执行。窗口外只有同时满足以下条件才写不可变授权并执行：

```python
allow_late_buys is True
and cycle.market == "CN"
and cycle.as_of_date == "2026-07-22"
and cycle.execution_date == "2026-07-23"
and local.date().isoformat() == cycle.execution_date
and cn_session(local) in {"morning", "afternoon"}
and trend_execution_mode(config, hostname_fn=socket.gethostname).mode == "execute"
```

授权文件写入 `data/trend_controller/CN/late_buy_authorizations/2026-07-23.json`，字段必须包含 schema、market、as_of_date、execution_date、report_path、report_sha256、actor、reason、authorized_at。重复调用只能读取完全相同授权；任何字段冲突都失败关闭。

把授权布尔值传入 `record_trend_review_missed_buys` 和 `execute_trend_review_open`。它只忽略买入窗口关闭，不绕过同日、市场开盘、报价、每手股数、现金、席位、账户、策略身份或幂等校验。

在 CLI 添加 `trend-market correct`；`--actor` 和 `--reason` 必填，`--allow-late-buys` 默认 false。不要把该开关加入常驻 `trend-market run`。

在 `CHANGELOG.md` 的最新日期下增加：

```markdown
- 趋势报告升级到 v5：行情、ATR14 和港股每手股数可在执行时补全；修订报告可在窗口内执行，并为 2026-07-23 A 股模拟盘提供一次性审计授权。
```

- [ ] **Step 5: 运行修订、CLI 和 Dashboard 聚焦测试**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_trend_review.py \
  tests/test_trend_market_controller.py \
  tests/test_dashboard.py \
  tests/test_trend_market_cli.py -q
```

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add \
  src/open_trader/trend_review.py \
  src/open_trader/trend_market_controller.py \
  src/open_trader/cli.py \
  src/open_trader/dashboard.py \
  CHANGELOG.md \
  tests/test_trend_review.py \
  tests/test_trend_market_controller.py \
  tests/test_dashboard.py \
  tests/test_trend_market_cli.py
git commit -m "feat: execute corrected trend report revisions"
```

---

### Task 5: 全量验证、真实修订报告、模拟买入与最终部署

**Files:**
- Modify only if a verified defect is found during the checks below
- Verify: `reports/trend_a_share/2026-07-22-r1.json`
- Verify: `reports/trend_a_share/2026-07-22-r1.md`
- Verify: `data/trend_review/ledgers/CN/`
- Verify: `data/trend_a_share/protection_state.json`
- Verify: `data/trend_controller/CN/`

**Interfaces:**
- Consumes: Tasks 1-4
- Produces: v5 修订报告、不可变授权、模拟盘订单事实、Feishu 状态、最终 acceptance 结果和精确 SHA 部署

- [ ] **Step 1: 运行所有自动测试**

Run:

```bash
.venv/bin/python -m pytest -q
```

Expected: `3166+` tests passed；零失败、零收集错误。

- [ ] **Step 2: 提交最终源码并确认干净 SHA**

```bash
git status --short
git diff --check
git rev-parse HEAD
```

Expected: worktree clean；记录 `ACCEPTED_CANDIDATE_SHA`。

- [ ] **Step 3: 检查旧进程，再运行真实修订与一次性模拟执行**

先只读检查：

```bash
screen -ls
launchctl list | rg 'com\.open-trader\.(trend|premarket)'
ps -axo pid,lstart,command | rg 'open_trader|trend-market'
```

从本工作树执行获批命令：

```bash
PYTHONPATH=src .venv/bin/python -m open_trader trend-drawdown-preflight \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env \
  --repo /Users/ray/projects/open_trader/.worktrees/trend-report-optional-market-data \
  --actor correction
PYTHONPATH=src .venv/bin/python -m open_trader trend-market correct \
  --market CN \
  --actor ray \
  --reason "2026-07-22 report excluded formal buys because Futu ATR data was unavailable" \
  --allow-late-buys \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env
```

Expected:

- 新建而非覆盖 `reports/trend_a_share/2026-07-22-r1.{json,md}`。
- 报告策略为 v5，`execution_date` 为 `2026-07-23`，不再出现 `atr_unavailable`。
- 最新修订的全部合法正式动作进入 Futu SIMULATE 动作账本；缺报价/每手股数的动作明确受阻。
- 每个提交订单都有唯一 intent/result、Futu remark 和 report SHA；重复运行不产生第二单。
- 无 ATR 成交写 `entry_fill_price` 与 `protection_status=pending`，Feishu 发送一次状态提醒。

用 `jq`、订单查询和新鲜日志核对报告哈希、订单 ID、数量、状态、时间戳；不要根据命令退出码推断成交。

- [ ] **Step 4: 重启仍加载旧 SHA 的控制器并验证新日志**

从当前工作树重装/重启 CN、HK、US controller：

```bash
scripts/install_daily_premarket_launchd.sh \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env \
  --trend-only --market all
launchctl print "gui/$(id -u)/com.open-trader.trend-market-controller.cn"
launchctl print "gui/$(id -u)/com.open-trader.trend-market-controller.hk"
launchctl print "gui/$(id -u)/com.open-trader.trend-market-controller.us"
ps -axo pid,lstart,command | rg 'trend-market run'
tail -n 100 logs/daily_premarket/launchd-trend-controller-*.out.log
```

Expected: 新 PID、工作目录为本工作树、进程 SHA 为 `ACCEPTED_CANDIDATE_SHA`，日志时间晚于重启时间；三市场各跑一次真实行情/账户刷新。若外部行情或浏览器不可用，记录真实 blocker，不用 fixture 替代。

- [ ] **Step 5: 运行唯一最终 Dashboard acceptance 门禁**

Run:

```bash
make acceptance
```

Expected: 最后一行 `PASS`。若为 `FAIL`，修复后重新从聚焦测试开始；若为 `BLOCKED`，停止并报告 blocker，不得声称完成。

- [ ] **Step 6: 以完全相同的已验收 SHA 重新部署**

确认没有源文件或数据生成器改动后，从 `ACCEPTED_CANDIDATE_SHA` 重启 controller 和 Dashboard：

```bash
export ACCEPTED_SHA="$(git rev-parse HEAD)"
test "$(git rev-parse HEAD)" = "$ACCEPTED_SHA"
test -z "$(git status --short)"
scripts/install_daily_premarket_launchd.sh \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env \
  --trend-only --market all
screen -S open_trader_dashboard_8766 -X quit 2>/dev/null || true
screen -dmS open_trader_dashboard_8766 zsh -lc \
  'cd /Users/ray/projects/open_trader/.worktrees/trend-report-optional-market-data && exec env PYTHONPATH=src .venv/bin/python -u -m open_trader dashboard --portfolio /Users/ray/projects/open_trader/data/latest/portfolio.csv --data-dir /Users/ray/projects/open_trader/data --reports-dir /Users/ray/projects/open_trader/reports --config /Users/ray/projects/open_trader/config/daily_premarket.env --poll-seconds 5 --host 127.0.0.1 --port 8766 >> /tmp/open_trader_dashboard_8766.log 2>&1'
screen -ls | rg 'open_trader_dashboard_8766'
ps -axo pid,lstart,command | rg 'open_trader dashboard|trend-market run'
tail -n 80 logs/daily_premarket/launchd-trend-controller-*.out.log
tail -n 80 /tmp/open_trader_dashboard_8766.log
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/
curl -sS http://127.0.0.1:8766/api/dashboard | \
  .venv/bin/python -m json.tool >/dev/null
```

Expected: SHA 与 acceptance 完全一致；PID/启动时间为新值；工作目录正确；新日志存在；HTTP 状态 `200`。最终提供 `http://127.0.0.1:8766/` 供评审。
