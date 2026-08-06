# Issue #32 短结算 + 盘口深度筛选取向 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让套利筛选按“年化 >15% 硬门槛 → 短结算优先 → 盘口深度充足”工作，低于 15% 的信号不在任何展示列表出现（后台统计保留），UI 展示结算期与可执行深度。

**Architecture:** 复用现有筛选管线，做四处增量：(1) 新增全簿正边际深度探针，同市场/跨市场机会行暴露理论深度与 $20 政策下单量双值；(2) Dashboard API 统一过滤低于门槛信号；(3) Dashboard API 排序改为“可参与 → 年化降序 → 结算期升序 → 绝对利润降序 → 成交量降序 → ID”；(4) 前端卡片展示结算期与深度，历史表展示结算期。执行/通知/健康检查不在本计划范围。

**Tech Stack:** Python 3.12, asyncio, Decimal, pytest, 现有 Dashboard API（`dashboard_web.py`）、Dashboard 前端（`dashboard_static/dashboard.js`）、Polymarket monitor、Predict 跨市场 monitor。

## Global Constraints

- 年化门槛常量：`MIN_THRESHOLD_ANNUALIZED_YIELD = Decimal("0.15")`（`src/open_trader/prediction_arbitrage.py:17`），不得改动。
- 低于门槛的信号后台统计必须保留（`annualized_distribution`、`signals_24h` 不变）。
- 本计划不改变 `MIN_ESTIMATED_PROFIT = Decimal("1.00")`、`MAX_NORMAL_COST = Decimal("20.00")`、`MAX_WALLET_BALANCE`（属于 Issue #33）。
- 本计划不改变飞书观察提醒（Issue #28 已合入 main）和健康检查（Issue #29）。
- 跨市场部分基于已合入 main 的 Issue #27（Gamma-only 配对解析）。
- 基准分支：本地 `main`（当前 `64809e3f`），独立 worktree 分支 `docs/issue-32-short-settlement-depth-screening`。
- 合并到 main 前必须更新 `CHANGELOG.md` 的日期条目（AGENTS.md Merge Log Gate）。
- Dashboard 相关改动最后必须跑 `make acceptance`，只有 `PASS` 才能交付 review。

## 已确认的产品决策（2026-08-07 grill 结论）

1. 深度口径 = 整条盘口、含手续费后净边际仍为正的最大可执行数量/金额，不受 $20 限制。
2. 达标（>15%）的长结算机会也展示，只排在后面；UI 显示“剩余 X 天 + 结算时间”。
3. 机会卡片显示结算期 + 深度双值（理论可执行深度 / 当前 $20 政策下单量）；历史信号表只显示结算期，不显示深度。
4. 历史表隐藏低于 15% / 年化无法计算的行；分布统计与 24h 计数保留。
5. 机会列表只隐藏“低于 15% / 年化无法计算”；其他暂不可参与行（LLM 未通过、盘口过期、熔断等）保留原因。
6. 排序：可参与置顶 → 年化降序 → 结算期升序 → 绝对利润降序 → 成交量降序 → ID；暂不可参与用同样次级规则排后面。
7. UI 形态（2026-08-07 mock 选定）：候选区使用**紧凑表格**（Variant C），同市场与跨市场机会统一进表；**移除预测市场工作区左侧的“可观察标的”组件**（`predictionRelationDiscoveryPanel` 所在 aside）。表格列：标的 / 年化 / 结算期 / 理论深度 / 政策下单量 / 状态 / 操作。

---

### Task 1: 全簿正边际深度探针（共享纯函数）

**Files:**
- Modify: `src/open_trader/polymarket_relation_discovery.py`（`_fee` 附近）
- Test: `tests/test_prediction_arbitrage.py`

**Interfaces:**
- Consumes: 现有 `_book_segments`、`_worst_price`、`_fee`。
- Produces:

```python
@dataclass(frozen=True, slots=True)
class PositiveEdgeDepth:
    quantity: Decimal
    cost: Decimal

def positive_edge_depth(
    segments_a: list[tuple[Decimal, Decimal, Decimal]],
    segments_b: list[tuple[Decimal, Decimal, Decimal]],
    *,
    tick_size_a: Decimal,
    tick_size_b: Decimal,
    fee_rate_a: Decimal,
    fee_rate_b: Decimal,
    minimum_order_size: Decimal,
    extra_cost: Decimal = Decimal("0"),
) -> PositiveEdgeDepth | None:
```

语义：返回两条腿盘口合并后，`quantity - cost_a - cost_b - fee_a - fee_b - extra_cost > 0` 的最大共同数量；`cost = cost_a + cost_b + fee_a + fee_b + extra_cost`。无正边际候选时返回 `None`。

- [ ] **Step 1: 写失败测试**

在 `tests/test_prediction_arbitrage.py` 新增：

```python
def test_positive_edge_depth_returns_largest_common_positive_edge() -> None:
    segments_a = [
        (Decimal("0.98"), Decimal("0"), Decimal("100")),
        (Decimal("0.99"), Decimal("100"), Decimal("500")),
    ]
    segments_b = [
        (Decimal("0.005"), Decimal("0"), Decimal("1000")),
    ]
    depth = positive_edge_depth(
        segments_a,
        segments_b,
        tick_size_a=Decimal("0.01"),
        tick_size_b=Decimal("0.005"),
        fee_rate_a=Decimal("0.002"),
        fee_rate_b=Decimal("0.002"),
        minimum_order_size=Decimal("1"),
    )
    assert depth is not None
    assert depth.quantity == Decimal("500")
    expected_cost = (
        Decimal("500") * Decimal("0.99")
        + Decimal("500") * Decimal("0.005")
        + _fee(Decimal("500"), Decimal("0.002"), Decimal("0.99"))
        + _fee(Decimal("500"), Decimal("0.002"), Decimal("0.005"))
    )
    assert depth.cost == expected_cost


def test_positive_edge_depth_returns_none_when_edge_turns_negative() -> None:
    segments_a = [(Decimal("0.999"), Decimal("0"), Decimal("1000"))]
    segments_b = [(Decimal("0.001"), Decimal("0"), Decimal("1000"))]
    assert (
        positive_edge_depth(
            segments_a,
            segments_b,
            tick_size_a=Decimal("0.001"),
            tick_size_b=Decimal("0.001"),
            fee_rate_a=Decimal("0.01"),
            fee_rate_b=Decimal("0.01"),
            minimum_order_size=Decimal("1"),
        )
        is None
    )


def test_positive_edge_depth_includes_extra_cost_for_cross_venue_gas() -> None:
    segments_a = [(Decimal("0.98"), Decimal("0"), Decimal("100"))]
    segments_b = [(Decimal("0.01"), Decimal("0"), Decimal("100"))]
    depth = positive_edge_depth(
        segments_a,
        segments_b,
        tick_size_a=Decimal("0.01"),
        tick_size_b=Decimal("0.01"),
        fee_rate_a=Decimal("0"),
        fee_rate_b=Decimal("0"),
        minimum_order_size=Decimal("1"),
        extra_cost=Decimal("0.50"),
    )
    assert depth is None


def test_positive_edge_depth_respects_minimum_order_size() -> None:
    segments_a = [(Decimal("0.98"), Decimal("0"), Decimal("100"))]
    segments_b = [(Decimal("0.01"), Decimal("0"), Decimal("100"))]
    assert (
        positive_edge_depth(
            segments_a,
            segments_b,
            tick_size_a=Decimal("0.01"),
            tick_size_b=Decimal("0.01"),
            fee_rate_a=Decimal("0"),
            fee_rate_b=Decimal("0"),
            minimum_order_size=Decimal("200"),
        )
        is None
    )
```

若测试文件尚未导入 `_fee` 和 `positive_edge_depth`，在 import 区补上。

- [ ] **Step 2: 运行确认失败**

Run: `PYTHONSAFEPATH=1 PYTHONPATH=src .venv/bin/python -m pytest tests/test_prediction_arbitrage.py -q -k "positive_edge_depth" -v`
Expected: FAIL，`ImportError: cannot import name 'positive_edge_depth'`。

- [ ] **Step 3: 实现**

在 `src/open_trader/polymarket_relation_discovery.py` 中 `_fee` 定义附近加入：

```python
@dataclass(frozen=True, slots=True)
class PositiveEdgeDepth:
    quantity: Decimal
    cost: Decimal


def positive_edge_depth(
    segments_a: list[tuple[Decimal, Decimal, Decimal]],
    segments_b: list[tuple[Decimal, Decimal, Decimal]],
    *,
    tick_size_a: Decimal,
    tick_size_b: Decimal,
    fee_rate_a: Decimal,
    fee_rate_b: Decimal,
    minimum_order_size: Decimal,
    extra_cost: Decimal = Decimal("0"),
) -> PositiveEdgeDepth | None:
    if not all(
        isinstance(value, Decimal) and value.is_finite() and value > 0
        for value in (tick_size_a, tick_size_b, minimum_order_size)
    ):
        return None
    if not all(
        isinstance(value, Decimal) and value.is_finite() and value >= 0
        for value in (fee_rate_a, fee_rate_b, extra_cost)
    ):
        return None
    depths_a = [total for _, _, total in segments_a]
    depths_b = [total for _, _, total in segments_b]
    if not depths_a or not depths_b:
        return None
    candidates = sorted(
        {min(left, right) for left in depths_a for right in depths_b},
        reverse=True,
    )
    for quantity in candidates:
        if quantity < minimum_order_size or quantity <= 0:
            continue
        price_a = _worst_price(segments_a, quantity)
        price_b = _worst_price(segments_b, quantity)
        if price_a is None or price_b is None:
            continue
        cost_a = quantity * price_a
        cost_b = quantity * price_b
        fee_a = _fee(quantity, fee_rate_a, price_a)
        fee_b = _fee(quantity, fee_rate_b, price_b)
        total_cost = cost_a + cost_b + fee_a + fee_b + extra_cost
        if quantity - total_cost > 0:
            return PositiveEdgeDepth(quantity=quantity, cost=total_cost)
    return None
```

（`tick_size_a`/`tick_size_b` 暂只用于类型校验；候选数量来自盘口段边界，天然满足两腿各自可成交深度。）

- [ ] **Step 4: 运行确认通过**

Run: `PYTHONSAFEPATH=1 PYTHONPATH=src .venv/bin/python -m pytest tests/test_prediction_arbitrage.py -q -k "positive_edge_depth" -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/polymarket_relation_discovery.py tests/test_prediction_arbitrage.py
git commit -m "feat: add full-book positive-edge depth probe (#32)"
```

---

### Task 2: 同市场阈值机会行暴露理论深度与政策下单量

**Files:**
- Modify: `src/open_trader/polymarket_monitor.py`（`_refresh_relation_rows` 调用处约 2525-2535 行、`_relation_row` 约 2859-3050 行）
- Test: `tests/test_polymarket_monitor.py`

**Interfaces:**
- Consumes: Task 1 的 `positive_edge_depth`、`_book_segments`；现有 `_relation_books`、`_threshold_candidate` 的 `intent`/`candidate`。
- Produces: 每个 `threshold_hedge` 机会行新增：
  - `depth_status: str`（`"pass"` / `"insufficient"`）
  - `max_executable_quantity: Decimal`（理论深度，无上限）
  - `max_executable_cost: Decimal`（理论深度对应含费成本）
  - `policy_quantity: Decimal`（当前 $20 政策下可下单数量，`intent.quantity`）
  - `policy_cost: Decimal`（当前 $20 政策下可下单成本，`intent.total_max_cost`）

- [ ] **Step 1: 写失败测试**

在 `tests/test_polymarket_monitor.py` 新增：

```python
def test_threshold_row_exposes_theoretical_and_policy_depth(
    tmp_path: Path,
) -> None:
    setup_public([threshold_event()])
    setup_threshold_books()
    validator = FakeRelationValidator()
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=validator,
    )

    asyncio.run(monitor._run_full_relation_scan(FakePublicClient()))
    monitor.refresh_once()

    row = next(
        row
        for row in monitor.snapshot()["opportunities"]
        if row.get("market_type") == "threshold_hedge"
    )
    assert row["depth_status"] == "pass"
    assert row["max_executable_quantity"] >= row["policy_quantity"]
    assert row["max_executable_cost"] >= row["policy_cost"]
    assert row["policy_quantity"] == row["quantity"]
    assert row["policy_cost"] == row["total_max_cost"]


def test_threshold_row_reports_insufficient_depth_when_no_positive_edge(
    tmp_path: Path,
) -> None:
    # 两腿价格之和等于 1，含费后无正边际（activity candidate 仍存在，行会被创建）。
    setup_public([threshold_event()])
    setup_threshold_books(low_ask="0.995", high_no_ask="0.005")
    validator = FakeRelationValidator()
    monitor = make_monitor(
        tmp_path,
        relation_discovery=discover_threshold_relations,
        relation_validator=validator,
    )

    asyncio.run(monitor._run_full_relation_scan(FakePublicClient()))
    monitor.refresh_once()

    row = next(
        row
        for row in monitor.snapshot()["opportunities"]
        if row.get("market_type") == "threshold_hedge"
    )
    assert row["depth_status"] == "insufficient"
    assert row["max_executable_quantity"] == Decimal("0")
    assert row["max_executable_cost"] == Decimal("0")
    assert row["policy_quantity"] == Decimal("0")
    assert row["policy_cost"] == Decimal("0")
    assert row["actionable"] is False
```

（若 `setup_threshold_books(low_ask=..., high_no_ask=...)` 的 `high_no_ask` 语义是 NO 腿 ask，保持与现有测试一致；否则用现有“无正边际”测试的数值。）

- [ ] **Step 2: 运行确认失败**

Run: `PYTHONSAFEPATH=1 PYTHONPATH=src .venv/bin/python -m pytest tests/test_polymarket_monitor.py -q -k "depth" -v`
Expected: FAIL，`KeyError: 'depth_status'`。

- [ ] **Step 3: 实现**

在调用 `_relation_row` 前（`safe_intent = build_threshold_hedge_intent(...)` 之后）计算理论深度：

```python
            probe = positive_edge_depth(
                _book_segments(
                    self._relation_books[relation.buy_leg_a.token_id].asks,
                    relation.market_a.tick_size,
                )
                or [],
                _book_segments(
                    self._relation_books[relation.buy_leg_b.token_id].asks,
                    relation.market_b.tick_size,
                )
                or [],
                tick_size_a=relation.market_a.tick_size,
                tick_size_b=relation.market_b.tick_size,
                fee_rate_a=_fee_rate(relation.market_a) or Decimal("0"),
                fee_rate_b=_fee_rate(relation.market_b) or Decimal("0"),
                minimum_order_size=max(
                    relation.market_a.minimum_order_size,
                    relation.market_b.minimum_order_size,
                ),
            )
```

并把 `_relation_row` 的 `selected = intent or candidate` 之后、row dict 的 `"planned_amount": selected.total_max_cost,` 之后加入：

```python
            "depth_status": "pass" if probe is not None else "insufficient",
            "max_executable_quantity": (
                probe.quantity if probe is not None else Decimal("0")
            ),
            "max_executable_cost": (
                probe.cost if probe is not None else Decimal("0")
            ),
            "policy_quantity": intent.quantity if intent is not None else Decimal("0"),
            "policy_cost": intent.total_max_cost if intent is not None else Decimal("0"),
```

`_relation_row` 签名增加 `probe: PositiveEdgeDepth | None` 参数。

- [ ] **Step 4: 运行确认通过**

Run: `PYTHONSAFEPATH=1 PYTHONPATH=src .venv/bin/python -m pytest tests/test_polymarket_monitor.py -q -k "depth" -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/polymarket_monitor.py tests/test_polymarket_monitor.py
git commit -m "feat: expose theoretical and policy depth on threshold hedge rows (#32)"
```

---

### Task 3: 跨市场机会暴露理论深度与政策下单量

**Files:**
- Modify: `src/open_trader/predict_cross_venue.py`（`_build_cross_venue_intents` 约 263-430 行、`_opportunity_payload` 约 1640-1700 行）
- Test: `tests/test_predict_cross_venue.py`

**Interfaces:**
- Consumes: Task 1 的 `positive_edge_depth`、`CROSS_VENUE_GAS_RESERVE`。
- Produces: 跨市场机会 payload 新增 `depth_status`、`max_executable_quantity`、`max_executable_cost`、`policy_quantity`、`policy_cost`（与 Task 2 同名字段）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_predict_cross_venue.py` 的 `test_cross_venue_intent_uses_decimal_depth_fees_later_settlement_and_venue_ids`（约 711 行）末尾追加：

```python
    payload = monitor.snapshot()["opportunities"][0]
    assert payload["depth_status"] == "pass"
    assert payload["max_executable_quantity"] >= payload["policy_quantity"]
    assert payload["max_executable_cost"] >= payload["policy_cost"]
    assert payload["policy_quantity"] == payload["quantity"]
    assert payload["policy_cost"] == payload["total_max_cost"]
```

若该测试没有 `monitor` 变量，则按该文件现有 `FakeCrossVenueValidator` + `monitor_gamma` 模式新增独立测试，构造成功后调用 `_discover()` 并断言 `snapshot()["opportunities"][0]` 的上述五个字段。

- [ ] **Step 2: 运行确认失败**

Run: `PYTHONSAFEPATH=1 PYTHONPATH=src .venv/bin/python -m pytest tests/test_predict_cross_venue.py -q -k "depth" -v`
Expected: FAIL，`KeyError: 'depth_status'`。

- [ ] **Step 3: 实现**

在 `_build_cross_venue_intents` 的 direction 循环内，`polymarket_segments` 计算之后、intent 循环之前加入理论探针：

```python
        depth_probe = positive_edge_depth(
            predict_side,
            polymarket_segments,
            tick_size_a=pair.predict.tick_size,
            tick_size_b=pair.polymarket.tick_size,
            fee_rate_a=pair.predict.fee_rate_bps / Decimal("10000"),
            fee_rate_b=pair.polymarket.fee_rate_bps / Decimal("10000"),
            minimum_order_size=minimum,
            extra_cost=CROSS_VENUE_GAS_RESERVE,
        )
```

在 `_opportunity_payload` 返回 dict 中 `"total_max_cost": intent.total_max_cost,` 之后加入：

```python
            "depth_status": "pass" if depth_probe is not None else "insufficient",
            "max_executable_quantity": (
                depth_probe.quantity if depth_probe is not None else Decimal("0")
            ),
            "max_executable_cost": (
                depth_probe.cost if depth_probe is not None else Decimal("0")
            ),
            "policy_quantity": intent.quantity,
            "policy_cost": intent.total_max_cost,
```

`_opportunity_payload` 签名增加 `depth_probe: PositiveEdgeDepth | None` 参数。

- [ ] **Step 4: 运行确认通过**

Run: `PYTHONSAFEPATH=1 PYTHONPATH=src .venv/bin/python -m pytest tests/test_predict_cross_venue.py -q -k "depth" -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/predict_cross_venue.py tests/test_predict_cross_venue.py
git commit -m "feat: expose theoretical and policy depth on cross-venue opportunities (#32)"
```

---

### Task 4: Dashboard API 隐藏低于 15% 年化的信号

**Files:**
- Modify: `src/open_trader/dashboard_web.py`（import 区约 11 行、`_prediction_state_payload` 约 920-945 行、`_prediction_history_payload` 约 1090-1100 行）
- Test: `tests/test_dashboard_web.py`

**Interfaces:**
- Consumes: `MIN_THRESHOLD_ANNUALIZED_YIELD`（从 `prediction_arbitrage` 导入）、`Decimal`。
- Produces: `_prediction_displayable(row) -> bool`；`/api/prediction-arbitrage/state` 的 `events`/`opportunities` 与 `/api/prediction-arbitrage/history?kind=signals` 的 `items` 不再包含低于门槛信号；`annualized_distribution`、`signals_24h` 不变。

- [ ] **Step 1: 写失败测试**

在 `tests/test_dashboard_web.py` 新增（fixture 类 `_StateFakeMonitor`、`_StateFakeExecution`、`_HistoryFakeStore` 与上一版计划相同，见下方）：

```python
def test_state_payload_hides_below_threshold_opportunities_and_markets() -> None:
    below = {
        "opportunity_id": "below",
        "market_type": "threshold_hedge",
        "annualized_yield": "0.05",
        "eligibility_reason": "annualized_yield_below_minimum",
        "actionable": False,
        "event_id": "ev-below",
        "quantity": "20",
        "total_max_cost": "19.9",
    }
    above = {
        "opportunity_id": "above",
        "market_type": "threshold_hedge",
        "annualized_yield": "0.20",
        "eligibility_reason": "actionable",
        "actionable": True,
        "event_id": "ev-above",
        "remaining_days": "3",
        "quantity": "20",
        "total_max_cost": "19.9",
    }
    state = {
        "status": "healthy",
        "health": {"status": "healthy", "degraded_reasons": []},
        "readiness": {"status": "ready", "geoblock": "allowed", "relayer": "ready"},
        "events": [],
        "opportunities": [below, above],
    }
    result = _prediction_state_payload(
        store=None,
        monitor=_StateFakeMonitor(state),
        execution=_StateFakeExecution(),
        csrf_token="",
    )
    assert [row["opportunity_id"] for row in result["opportunities"]] == ["above"]


def test_signal_history_hides_below_threshold_rows() -> None:
    rows = [
        {
            "signal_id": "below",
            "market_type": "threshold_hedge",
            "annualized_yield": "0.007",
            "eligibility_reason": "annualized_yield_below_minimum",
            "started_at": "2026-08-06T00:00:00Z",
            "occurred_at": "2026-08-06T00:00:00Z",
        },
        {
            "signal_id": "above",
            "market_type": "threshold_hedge",
            "annualized_yield": "0.20",
            "eligibility_reason": "actionable",
            "started_at": "2026-08-06T00:01:00Z",
            "occurred_at": "2026-08-06T00:01:00Z",
            "opportunity_id": "above",
        },
    ]
    payload = _prediction_history_payload(
        _HistoryFakeStore(rows),
        kind="signals",
        limit=10,
        offset=0,
    )
    assert [row["signal_id"] for row in payload["items"]] == ["above"]
```

测试文件工具区加入：

```python
class _StateFakeMonitor:
    def __init__(self, snapshot_value: dict[str, object]) -> None:
        self._snapshot_value = snapshot_value

    def snapshot(self) -> dict[str, object]:
        return self._snapshot_value


class _StateFakeExecution:
    _breaker_open = False
    _cross_breaker_open = False


class _HistoryFakeStore:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows
        self.cache_hits = 0

    def histories(self, kind: str) -> list[dict[str, object]]:
        assert kind == "signals"
        return self._rows

    def active_execution(self) -> None:
        return None

    def unacknowledged_incident(self) -> None:
        return None

    def signal_history(self, _window: str) -> list[dict[str, object]]:
        return self._rows

    def load_runtime(self) -> dict[str, object]:
        return {}

    def load_llm_cache(self, _key: str) -> dict[str, object] | None:
        return None

    def record_llm_cache_hit(self) -> None:
        type(self).cache_hits += 1
```

- [ ] **Step 2: 运行确认失败**

Run: `PYTHONSAFEPATH=1 PYTHONPATH=src .venv/bin/python -m pytest tests/test_dashboard_web.py -q -k "hides_below_threshold" -v`
Expected: FAIL（`opportunities` 仍含 `below`）。

- [ ] **Step 3: 实现**

`src/open_trader/dashboard_web.py` 顶部 import 改为 `from datetime import UTC, date, datetime`，并从 `prediction_arbitrage` import 区加入 `MIN_THRESHOLD_ANNUALIZED_YIELD`（若尚未导入）。新增 helper：

```python
def _prediction_displayable(row: Mapping[str, object]) -> bool:
    reason = str(row.get("eligibility_reason") or "").strip()
    if reason in {"annualized_yield_below_minimum", "annualized_yield_unavailable"}:
        return False
    annualized = row.get("annualized_yield")
    if annualized not in (None, ""):
        try:
            value = Decimal(str(annualized))
        except Exception:
            value = None
        if value is not None and value.is_finite() and value < MIN_THRESHOLD_ANNUALIZED_YIELD:
            return False
    return True
```

在 `_prediction_state_payload` 中，`opportunity_rows.extend(...)` 之后、`event_rows = sorted(...)` 之前改为：

```python
    opportunity_rows = [
        row for row in opportunity_rows if _prediction_displayable(row)
    ]
    for event in event_rows:
        markets = event.get("markets")
        if isinstance(markets, (list, tuple)):
            event["markets"] = [
                market
                for market in markets
                if isinstance(market, Mapping) and _prediction_displayable(market)
            ]
    event_rows = [
        event
        for event in event_rows
        if event.get("markets") or event.get("actionable") is True
    ]
```

在 `_prediction_history_payload` 的 `if kind == "signals":` 分支开头加入：

```python
        safe_rows = [
            row
            for row in safe_rows
            if not isinstance(row, Mapping) or _prediction_displayable(row)
        ]
```

- [ ] **Step 4: 运行确认通过**

Run: `PYTHONSAFEPATH=1 PYTHONPATH=src .venv/bin/python -m pytest tests/test_dashboard_web.py -q -k "hides_below_threshold" -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/dashboard_web.py tests/test_dashboard_web.py
git commit -m "feat: hide below-threshold signals from dashboard state and history (#32)"
```

---

### Task 5: 排序改为“可参与 → 年化 → 结算期 → 利润 → 成交量 → ID”

**Files:**
- Modify: `src/open_trader/dashboard_web.py`（`_prediction_sort_key` 约 201-235 行）
- Test: `tests/test_dashboard_web.py`

**Interfaces:**
- Consumes: 行/事件上的 `annualized_yield`、`remaining_days`；跨市场行上的 `resolution_at`/`canonical_cutoff`。
- Produces: `_prediction_sort_key` 返回 `tuple[bool, Decimal, Decimal, Decimal, Decimal, Decimal, str]`：`(not actionable, -annualized, remaining_days, -profit, -volume, -gross_upper_bound?, event_id)`。

- [ ] **Step 1: 写失败测试**

```python
def test_prediction_sort_key_orders_actionable_then_annualized_then_settlement() -> None:
    rows = [
        {
            "event_id": "low-annualized-short",
            "actionable": True,
            "annualized_yield": "0.16",
            "remaining_days": "1",
            "profit": "1.00",
            "volume_24h": "1",
        },
        {
            "event_id": "high-annualized-long",
            "actionable": True,
            "annualized_yield": "0.80",
            "remaining_days": "40",
            "profit": "1.00",
            "volume_24h": "1",
        },
        {
            "event_id": "high-annualized-short",
            "actionable": True,
            "annualized_yield": "0.80",
            "remaining_days": "3",
            "profit": "0.50",
            "volume_24h": "1",
        },
        {
            "event_id": "inactive",
            "actionable": False,
            "annualized_yield": "1.00",
            "remaining_days": "1",
            "profit": "1.00",
            "volume_24h": "1",
        },
    ]
    ordered = sorted(rows, key=_prediction_sort_key)
    assert [row["event_id"] for row in ordered] == [
        "high-annualized-short",
        "high-annualized-long",
        "low-annualized-short",
        "inactive",
    ]


def test_prediction_sort_key_falls_back_to_cross_venue_cutoff() -> None:
    short_cross = {
        "event_id": "cross-short",
        "actionable": True,
        "market_type": "cross_venue_yes_no",
        "annualized_yield": "0.50",
        "canonical_cutoff": "2026-08-10T00:00:00Z",
        "profit": "1.00",
        "volume_24h": "1",
    }
    long_cross = {
        "event_id": "cross-long",
        "actionable": True,
        "market_type": "cross_venue_yes_no",
        "annualized_yield": "0.50",
        "canonical_cutoff": "2026-12-31T00:00:00Z",
        "profit": "1.00",
        "volume_24h": "1",
    }
    assert _prediction_sort_key(short_cross) < _prediction_sort_key(long_cross)
```

（cutoff 日期必须相对测试运行时间在未来；若 CI 时间接近，用 `datetime.now(UTC) + timedelta(days=1)` 动态构造。）

- [ ] **Step 2: 运行确认失败**

Run: `PYTHONSAFEPATH=1 PYTHONPATH=src .venv/bin/python -m pytest tests/test_dashboard_web.py -q -k "sort_key" -v`
Expected: FAIL（顺序不符合）。

- [ ] **Step 3: 实现**

新增 `_prediction_annualized` 与 `_prediction_remaining_days` helper，并重写 `_prediction_sort_key`：

```python
def _prediction_annualized(item: Mapping[str, object]) -> Decimal:
    value = item.get("annualized_yield")
    if value not in (None, ""):
        try:
            parsed = Decimal(str(value))
        except Exception:
            parsed = None
        if parsed is not None and parsed.is_finite():
            return parsed
    opportunities = item.get("opportunities")
    if isinstance(opportunities, (list, tuple)):
        values = [
            _prediction_annualized(row)
            for row in opportunities
            if isinstance(row, Mapping)
        ]
        if values:
            return max(values)
    return Decimal("-Infinity")


def _prediction_remaining_days(item: Mapping[str, object]) -> Decimal:
    value = item.get("remaining_days")
    if value not in (None, ""):
        try:
            parsed = Decimal(str(value))
        except Exception:
            parsed = None
        if parsed is not None and parsed.is_finite():
            return parsed
    cutoff = item.get("resolution_at") or item.get("canonical_cutoff")
    if isinstance(cutoff, str):
        try:
            end = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
            days = Decimal(
                str((end.astimezone(UTC) - datetime.now(UTC).astimezone(UTC)).total_seconds())
            ) / Decimal("86400")
            if days.is_finite():
                return days
        except Exception:
            pass
    opportunities = item.get("opportunities")
    if isinstance(opportunities, (list, tuple)):
        days = [
            _prediction_remaining_days(row)
            for row in opportunities
            if isinstance(row, Mapping)
        ]
        if days:
            return min(days)
    return Decimal("Infinity")


def _prediction_sort_key(
    item: Mapping[str, object],
) -> tuple[bool, Decimal, Decimal, Decimal, Decimal, str]:
    opportunities = item.get("opportunities")
    actionable = bool(item.get("actionable"))
    nested_profits: list[Decimal] = []
    nested_volumes: list[Decimal] = []
    if isinstance(opportunities, (list, tuple)):
        for row in opportunities:
            if not isinstance(row, Mapping):
                continue
            actionable = actionable or row.get("actionable") is True
            nested_profits.append(
                _prediction_decimal_sort(row.get("profit", row.get("minimum_profit")))
            )
            nested_volumes.append(_prediction_decimal_sort(row.get("volume_24h")))
    profit = item.get("profit", item.get("gross_upper_bound"))
    volume = item.get("volume_24h")
    if profit is None and nested_profits:
        profit = max(nested_profits)
    if volume is None and nested_volumes:
        volume = max(nested_volumes)
    return (
        not actionable,
        -_prediction_annualized(item),
        _prediction_remaining_days(item),
        -_prediction_decimal_sort(profit),
        -_prediction_decimal_sort(volume),
        str(item.get("event_id") or ""),
    )
```

- [ ] **Step 4: 运行确认通过**

Run: `PYTHONSAFEPATH=1 PYTHONPATH=src .venv/bin/python -m pytest tests/test_dashboard_web.py -q -k "sort_key" -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/dashboard_web.py tests/test_dashboard_web.py
git commit -m "feat: sort opportunities by annualized then settlement then profit (#32)"
```

---

### Task 6: 前端候选区改为紧凑表格并移除观察标的组件

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js`（`predictionYesNoWorkspace` 约 3196-3211 行；可删除 `predictionRelationDiscoveryPanel` 调用；新增表格渲染函数）
- Test: `tests/test_dashboard_web.py`（`run_dashboard_js` 渲染测试）

**Interfaces:**
- Consumes: API 新增字段 `remaining_days`、`resolution_at`、`max_executable_quantity`、`max_executable_cost`、`policy_quantity`、`policy_cost`、`depth_status`；Task 5 已排序的 `opportunities`。
- Produces: 候选区渲染为一张紧凑表格（复用 `.pm-table`），列：标的（含 venue/类型 sub 标签）/ 年化 / 结算期（剩余 X 天 + 结算时间）/ 理论深度（“X 份 / $Y”或“深度不足”）/ 政策下单量 / 状态（可参与 / 仅观察 + 原因）/ 操作；预测市场工作区不再渲染左侧“可观察标的”aside。

- [ ] **Step 1: 写失败测试**

在 `tests/test_dashboard_web.py` 中找现有 `run_dashboard_js` 的预测市场工作区渲染测试，新增断言：

```python
def test_prediction_workspace_renders_candidate_table_and_no_observation_aside() -> None:
    output = run_dashboard_js(r'''
state.predictionMarket.payload = {
  status: "healthy",
  health: {status: "healthy", degraded_reasons: []},
  readiness: {status: "ready", geoblock: "allowed", relayer: "ready"},
  policy_limits: {max_wallet_balance: "65", max_normal_cost: "20", max_emergency_loss: "2", min_estimated_profit: "1"},
  breaker: {open: false},
  events: [],
  opportunities: [{
    opportunity_id: "threshold-1", market_type: "threshold_hedge",
    question: "A / B", question_a: "A", question_b: "B",
    relation: "B_IMPLIES_A", condition_id_a: "a", condition_id_b: "b",
    token_id_a: "ta", token_id_b: "tb",
    annualized_yield: "0.20", remaining_days: "3", resolution_at: "2026-08-10T00:00:00Z",
    max_executable_quantity: "2000", max_executable_cost: "1998.00",
    policy_quantity: "20", policy_cost: "19.90",
    depth_status: "pass", actionable: true,
    quantity: "20", total_max_cost: "19.90", minimum_payout: "20",
    profit: "0.10", llm_status: "approved",
    volume_24h: "29379",
  }],
};
const html = predictionYesNoWorkspace(state.predictionMarket.payload, new Set());
console.log(JSON.stringify(html));
''')
    rendered = json.loads(output)
    assert "候选标的" in rendered
    assert "年化" in rendered and "结算期" in rendered
    assert "理论深度" in rendered and "政策下单量" in rendered
    assert "2000" in rendered and "19.90" in rendered
    assert "剩余 3 天" in rendered
    assert "可观察标的" not in rendered
    assert "pm-llm-layout" not in rendered
```

- [ ] **Step 2: 运行确认失败**

Run: `PYTHONSAFEPATH=1 PYTHONPATH=src .venv/bin/python -m pytest tests/test_dashboard_web.py -q -k "candidate_table" -v`
Expected: FAIL（HTML 中无表格列头，或仍含 `pm-llm-layout`）。

- [ ] **Step 3: 实现**

在 `dashboard.js` 中新增表格渲染函数（放在 `predictionYesNoWorkspace` 附近）：

```js
function predictionCandidateTable(opportunities) {
  const rows = (Array.isArray(opportunities) ? opportunities : []).map((raw) => {
    const opportunity = predictionOpportunityDisplay(raw);
    const actionable = opportunity.actionable === true;
    const cross = predictionIsCrossVenue(opportunity);
    const title = predictionValue(opportunity.title_zh || opportunity.title || opportunity.question, "数据未返回");
    const sub = cross ? "Predict × Polymarket" : "Polymarket 阈值对冲";
    const annualized = predictionAnnualizedPercent(opportunity.annualized_yield, 2);
    const remaining = Number(opportunity.remaining_days);
    const settlement = Number.isFinite(remaining) && remaining > 0
      ? `${Number.isInteger(remaining) ? remaining : remaining.toFixed(1)} 天`
      : "不可计算";
    const resolution = predictionHktTimestamp(opportunity.resolution_at ?? opportunity.canonical_cutoff, "—");
    const depthOk = String(opportunity.depth_status || "") === "pass";
    const depth = depthOk
      ? `${escapeHtml(predictionValue(opportunity.max_executable_quantity, "-"))} 份 / ${escapeHtml(predictionMoney(opportunity.max_executable_cost))}`
      : `<span class="pm-tone-danger">深度不足</span>`;
    const policy = depthOk
      ? `${escapeHtml(predictionValue(opportunity.policy_quantity, "-"))} 份 / ${escapeHtml(predictionMoney(opportunity.policy_cost))}`
      : "—";
    const status = actionable
      ? `<span class="pm-pill action">可参与</span>`
      : `<span class="pm-pill watch">仅观察</span><small>${escapeHtml(predictionReasonLabel(opportunity.eligibility_reason || "opportunity_unavailable"))}</small>`;
    const action = actionable
      ? `<button class="pm-button primary pm-participate" type="button" data-action="participate" data-opportunity-id="${escapeHtml(predictionValue(opportunity.opportunity_id || opportunity.id, ""))}">确认</button>`
      : "—";
    return `<tr><td><strong>${escapeHtml(title)}</strong><span class="sub">${escapeHtml(sub)}</span></td><td class="num"><strong>${escapeHtml(annualized)}</strong></td><td class="num"><strong>${escapeHtml(settlement)}</strong><span class="sub">${escapeHtml(resolution)}</span></td><td class="num">${depth}</td><td class="num">${escapeHtml(policy)}</td><td>${status}</td><td>${action}</td></tr>`;
  }).join("");
  return `<div class="pm-table-wrap"><table class="pm-table"><thead><tr><th>标的</th><th class="num">年化</th><th class="num">结算期</th><th class="num">理论深度</th><th class="num">政策下单量</th><th>状态</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}
```

把 `predictionYesNoWorkspace` 的返回改为（移除 `predictionRelationDiscoveryPanel` aside 与卡片候选区，换成表格）：

```js
  return `${predictionRelationFunnel(payload)}<aside class="pm-policy"><strong>所有正收益候选都会展示</strong><p>低于 15% 年化的信号不展示；Codex 结论和程序复核全部通过后才出现人工确认入口；两腿属于不同 condition，不会 merge。</p></aside><section class="pm-panel"><header class="pm-panel-heading"><div><h2>候选标的</h2><p>按可参与 → 年化 → 结算期 → 利润排序；点击确认前会重新检查价格。</p></div><span class="pm-pill">显示 ${opportunities.length}</span></header>${predictionCandidateTable(opportunities)}</section>`;
```

历史表不需要深度，只保留现有结算期展示。若 `dashboard.css` 缺少 `.pm-table-wrap`/`.num`/`.sub` 样式，补充少量 CSS（`.num { text-align: right; }`、`.sub { color: var(--muted); display: block; font-size: 11px; }`、`.pm-table-wrap { overflow-x: auto; }`）。

- [ ] **Step 4: 运行确认通过**

Run: `PYTHONSAFEPATH=1 PYTHONPATH=src .venv/bin/python -m pytest tests/test_dashboard_web.py -q -k "candidate_table" -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/dashboard_static/dashboard.js tests/test_dashboard_web.py
git commit -m "feat: render prediction candidates as compact table without observation aside (#32)"
```

---

### Task 7: 回归测试、CHANGELOG 与 acceptance 门禁

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: 跑聚焦测试**

Run:
```bash
PYTHONSAFEPATH=1 PYTHONPATH=src .venv/bin/python -m pytest tests/test_prediction_arbitrage.py tests/test_polymarket_monitor.py tests/test_predict_cross_venue.py tests/test_dashboard_web.py -q
```
Expected: 全绿。

- [ ] **Step 2: 跑全量测试**

Run: `PYTHONSAFEPATH=1 PYTHONPATH=src .venv/bin/python -m pytest -q`
Expected: 全绿，且没有因排序键元组变化导致的断言失败（若有，更新对应测试期望）。

- [ ] **Step 3: 更新 CHANGELOG**

在 `CHANGELOG.md` 顶部按现有格式加入 `2026-08-07` 条目，说明 Issue #32 的筛选/展示/排序/深度行为，并引用本分支 commit。

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs: changelog for issue #32 short-settlement depth screening (#32)"
```

- [ ] **Step 5: 最终 acceptance 门禁（由实现者执行）**

Run: `make acceptance`
Expected: `PASS`。若 `FAIL`，回到对应任务修复后重跑；若 `BLOCKED`，如实报告阻塞，不得用单测替代。

- [ ] **Step 6: 部署已接受 SHA 并验证**

按 AGENTS.md：`make acceptance` PASS 后，redeploy 精确 SHA，验证新进程 PID、cwd、Git SHA、fresh logs、`curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8766/` 为 `200`，然后提供 review URL。

---

## Self-Review

**Spec coverage:**
- 年化 >15% 硬门槛：已有门槛逻辑；Task 4 保证门槛以下不展示（state、events、history）。
- 短结算优先 + 长结算仍展示：Task 5 排序，Task 6 UI 展示结算期。
- 全簿正边际深度（A 口径）：Task 1 共享探针，Task 2/3 暴露理论深度；当前 $20 政策下单量作为 `policy_*` 同时展示。
- 深度不足不进 actionable：现有 `intent is None → not actionable` 不变，Task 2/3 用 `depth_status` 显式化。
- 后台保留统计：Task 4 只过滤 API 展示，不改 `annualized_distribution`/`signals_24h`。
- 跨市场同样适用：Task 3 + Task 4/5/6 的 API/前端逻辑对跨市场统一生效。
- UI 展示结算期与深度：Task 6。
- 健康检查展示：不在本计划，归 Issue #29。

**Placeholder scan:** 无 TBD/TODO；所有代码步骤给出实际实现。

**Type consistency:** `_prediction_sort_key` 返回 7 元组与实现一致；`positive_edge_depth` 的 `PositiveEdgeDepth` 字段在 Task 1/2/3 中一致；`policy_quantity`/`policy_cost`/`max_executable_*`/`depth_status` 字段名在 API 与前端一致。
