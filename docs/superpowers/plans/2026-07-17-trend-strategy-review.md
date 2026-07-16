# 趋势交易系统复盘 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为东方财富 A 股、富途美股和辉立港股建立按 30 笔完整纪律模拟交易分批的复盘，展示当前策略完整参数，以及纪律模拟、实际执行和市场基准的期间净收益率、相对市场超额收益、最大回撤、卡玛比率和夏普比率。

**Architecture:** 现有趋势报告继续是动作和策略规则的唯一事实源，在冻结 JSON 中新增完整 `strategy_snapshot` 和可重放证据引用。新增一个 `trend_review.py` 负责 Futu 模拟账户动作、日终快照、不可变批次、纠正回放和五项指标；现有 watcher 在开盘和保护线触发点调用它，Dashboard 只读取 `data/latest/trend_review_cn.json`、`trend_review_us.json`、`trend_review_hk.json`。前端复用现有趋势报告工作区，以原生 HTML/CSS 绘制两组条形图，不增加依赖或第二套页面框架。

**Tech Stack:** Python 3.12、标准库 `dataclasses`/`decimal`/`hashlib`/`json`/`pathlib`、现有 Futu OpenD 客户端、现有 DGS3MO 利率数据、pytest、原生 JavaScript/CSS、Playwright Dashboard acceptance。

## Global Constraints

- 只实现批准的三个市场：东方财富 A 股、富途美股、辉立港股；老虎不显示复盘入口。
- 入口只放在对应账户头部 `当天趋势报告` 旁，文字固定为 `A 股复盘`、`美股复盘`、`港股复盘`。
- A 股只对比中证全指前复权收盘价，美股只对比 SPY 前复权收盘价，港股只对比恒生综合指数前复权收盘价；不同市场不得合并。
- 每个市场使用独立 Futu 模拟账户，并以全现金、零持仓开始；账户中不得混入其他实验。
- A 股和港股仅在连续交易时段提交市价单；买入数量按模拟账户当时净值与目标仓位重新计算。
- 批次固定为不重叠的 30 笔完整纪律模拟交易；版本归属以入场时快照为准，不追溯改写。
- 主页面只显示完整策略参数和五项指标：期间净收益率、相对市场超额收益、最大回撤、卡玛比率、夏普比率。
- 每项指标同时显示纪律模拟、实际执行、市场基准；缺数据时显示 `数据不足`，不得补猜或输出 `NaN`/无穷大。
- 不显示结论卡、运行状态卡、回测流程、参数编辑、参数导出、回测按钮、缺陷回放入口、Alpha、Beta、Sortino、胜率或盈亏比。
- 本期不实现“导出参数到回测”；运行版本不可从复盘页修改。
- 实际执行曲线使用趋势报告冻结的真实账户净值，因此自动包含纪律外交易；纪律外交易不得进入纪律模拟交易数。
- 纪律曲线直接采用三个富途模拟账户的权威日终净值，不再叠加手工费率，避免对模拟盘已计费用重复扣减。
- 基准通过 Futu 行情接口按复盘日冻结：`SH.000985`、`US.SPY`、`HK.800701`；每日事实同时保存日期、前复权收盘价、来源 ID 和 Futu 标的，不维护外部基准 CSV。
- 不增加第三方依赖；复用现有 Futu、原子 JSON 写入、DGS3MO 和 Dashboard 样式。
- 开发中只运行聚焦测试和直接工作流；`make acceptance` 只能作为最终门。
- `make acceptance` 只有 `PASS` 才能请求用户验收；随后必须部署相同已验收 Git SHA，并核对 PID、cwd、SHA、新日志和 Review URL HTTP 200。

---

## File Map

- Create: `src/open_trader/trend_review.py` — 复盘 schema、模拟盘动作、日终快照、批次、指标、证据冻结和纠正回放。
- Create: `tests/test_trend_review.py` — 复盘领域逻辑、不可变产物、模拟订单、批次和指标测试。
- Modify: `src/open_trader/a_share_trend.py` — 将现有规则常量化，生成完整策略快照，冻结 A 股重放证据。
- Modify: `src/open_trader/market_trend.py` — 生成港美策略快照并冻结同构证据。
- Modify: `纪律.md` — 同步当前真实运行的 A/美/港趋势规则与参数，仅作人读说明，不作为运行时配置源。
- Modify: `tests/test_a_share_trend.py` and `tests/test_market_trend.py` — 快照与证据投影回归。
- Modify: `src/open_trader/kelly_order_execution.py` — 让已有 Futu SIMULATE 客户端兼容市价单及账户/订单快照；Kelly 限价单默认行为保持不变。
- Modify: `tests/test_kelly_order_execution.py` — 市价单和快照兼容回归。
- Modify: `src/open_trader/tiger_long_term_backtest.py` — 将已有 `_portfolio_metrics` 提升为共享 `portfolio_metrics`。
- Modify: `tests/test_tiger_long_term_backtest.py` — 公共指标函数的边界测试。
- Modify: `src/open_trader/daily_premarket.py` and `config/daily_premarket.env.example` — 三个互不相同的模拟账户 ID。
- Modify: `src/open_trader/a_share_trend_watch.py` and `src/open_trader/market_trend_watch.py` — 开盘动作与保护线触发回调。
- Modify: `src/open_trader/cli.py` — `trend-review open|close|replay` 直接工作流，并接入现有报告/watcher 命令。
- Modify: `tests/test_daily_premarket.py`, `tests/test_a_share_trend_watch.py`, `tests/test_market_trend_watch.py`, `tests/test_premarket_cli.py` — 配置、时窗、幂等和 CLI 测试。
- Modify: `src/open_trader/dashboard.py` — 读取并严格校验三个最新复盘投影。
- Modify: `tests/test_dashboard.py` — Dashboard 复盘数据投影和错误降级。
- Modify: `src/open_trader/dashboard_static/dashboard.js` — 账户内入口、完整参数表、两组条形图和返回行为。
- Modify: `src/open_trader/dashboard_static/dashboard.css` — 复用现有 token 的条形图和 375px 参数布局。
- Modify: `tests/test_dashboard_web.py` and `tests/e2e/dashboard-warm-ledger.spec.ts` — 前端交互、转义、指标白名单和移动布局。
- Modify: `src/open_trader/dashboard_acceptance.py` and `tests/test_dashboard_acceptance.py` — 真实桌面/移动复盘验收。

---

### Task 1: 冻结当前策略身份与完整参数

**Files:**
- Modify: `src/open_trader/a_share_trend.py:31-61,493-660,793-990,1480-1515`
- Modify: `src/open_trader/market_trend.py:31-70,350-600`
- Modify: `纪律.md`
- Modify: `src/open_trader/daily_premarket.py:81-117,190-275`
- Modify: `config/daily_premarket.env.example`
- Test: `tests/test_a_share_trend.py`
- Test: `tests/test_market_trend.py`
- Test: `tests/test_daily_premarket.py`

**Interfaces:**
- Consumes: current `_candidate_reasons`, `_candidate_sort_key`, `estimate_buy_actions`, `_holding_action`, `update_protection_line`, `_process_version`.
- Produces: `trend_strategy_snapshot(market: str, process_version: str, candidate_pool_ids: Sequence[int]) -> dict[str, object]` and report JSON field `strategy_snapshot` consumed by every later task.

- [ ] **Step 1: Write failing snapshot tests**

Add tests that compare the snapshot to the actual runtime gates instead of a prose copy:

```python
def test_cn_strategy_snapshot_contains_every_runtime_parameter() -> None:
    snapshot = trend_strategy_snapshot(
        "CN", "abc123", Decimal("8.5"), Decimal("58.5"), (622466, 697199)
    )
    assert {key: snapshot[key] for key in (
        "strategy_id", "strategy_name", "strategy_version", "market",
        "effective_from", "process_version",
    )} == {
        "strategy_id": "trend_animals_warm_to_hot/CN/v1",
        "strategy_name": "A 股短线右侧趋势",
        "strategy_version": "v1",
        "market": "CN",
        "effective_from": "2026-07-14",
        "process_version": "abc123",
    }
    assert snapshot["parameters"] == {
            "candidate_pool_ids": [622466, 697199],
            "allowed_exchanges": ["SH", "SZ"],
            "excluded_name_markers": ["ST", "退"],
            "temperature_transition": {"from": ["温"], "to": ["热", "沸"]},
            "max_filter_price": "200",
            "min_strength": "95",
            "allowed_industry_temperatures": ["热", "沸"],
            "allowed_phases": ["谷雨", "立夏", "夏至"],
            "min_market_cap_100m": "100",
            "min_amount_100m": "2",
            "requires_right_side": True,
            "requires_tradable": True,
            "requires_no_danger": True,
            "requires_matching_data_date": True,
            "requires_not_held": True,
            "requires_right_side_days": True,
            "requires_atr14": True,
            "sort": ["strength_desc", "days_asc", "amount_desc", "symbol_asc"],
            "candidate_limit": 10,
            "position_limit": 10,
            "target_weight": {"热": "0.04", "沸": "0.02"},
            "lot_size": 100,
            "buy_window": "09:30-10:00",
            "initial_protection_atr_multiple": "2",
            "exit_reasons": ["danger", "left_right_side", "temperature_to_flat", "protection"],
            "trailing_low_days": 5,
            "protection_line_non_decreasing": True,
    }
    assert snapshot["parameter_rows"][0] == {
        "group": "候选来源", "name": "趋势动物组合", "value": "温转热（A 股）、温转热（ETF 基金个股）"
    }
    assert all(set(row) == {"group", "name", "value"} for row in snapshot["parameter_rows"])
```

Add exact US/HK assertions. Both snapshots contain `min_strength_exclusive="90"`, `max_right_side_days_exclusive=10`, `min_amount_100m="1"`, `requires_right_side=true`, `requires_tradable=true`, `requires_no_danger=true`, `requires_matching_data_date=true`, `requires_not_held=true`, `requires_atr14=true`, the same four-field sort, candidate/position limit 10, target weight `0.04`, initial protection multiple 2, trailing low days 5 and a non-decreasing protection line. US additionally contains `allowed_exchange="US"`, `lot_size=1`, `buy_window="美股常规交易时段"`; HK contains `allowed_exchange="HK"`, `lot_size_source="Futu 每标的整手"`, `buy_window="09:30-10:00"`. Each snapshot freezes the configured market pool IDs. Assert all `parameter_rows` group/name/value strings are Chinese and `_report_payload(report)["strategy_snapshot"]` equals the object attached at generation time.

In `tests/test_daily_premarket.py`, load a fixture containing all three positive, distinct simulate account IDs and assert `DailyPremarketConfig` preserves the exact IDs. Load the example's three empty values and assert unrelated configuration loading still succeeds.

- [ ] **Step 2: Run snapshot tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_a_share_trend.py -k strategy_snapshot \
  tests/test_market_trend.py -k strategy_snapshot
```

Expected: FAIL because reports do not contain `strategy_snapshot` and rule literals are not exposed as one snapshot.

- [ ] **Step 3: Replace duplicated literals with named constants and build the snapshot**

Add constants beside the existing field sets and use them in both runtime comparisons and serialization:

```python
CN_MAX_FILTER_PRICE = Decimal("200")
CN_MIN_STRENGTH = Decimal("95")
CN_MIN_MARKET_CAP_100M = Decimal("100")
CN_MIN_AMOUNT_100M = Decimal("2")
MARKET_MIN_STRENGTH_EXCLUSIVE = Decimal("90")
MARKET_MAX_RIGHT_SIDE_DAYS_EXCLUSIVE = 10
MARKET_MIN_AMOUNT_100M = Decimal("1")
POSITION_LIMIT = 10
CANDIDATE_LIMIT = 10
CN_TARGET_WEIGHTS = {"热": Decimal("0.04"), "沸": Decimal("0.02")}
DEFAULT_TARGET_WEIGHT = Decimal("0.04")
INITIAL_PROTECTION_ATR_MULTIPLE = Decimal("2")
TRAILING_LOW_DAYS = 5
```

Add this final required field to `TrendReport`:

```python
strategy_snapshot: dict[str, object]
```

Add this exact item to the existing `_report_payload` dictionary:

```python
"strategy_snapshot": _json_value(report.strategy_snapshot),
```

Call `trend_strategy_snapshot` from both report generators using the live `process_version` and actual configured pool IDs. The function returns both machine-readable `parameters` and Chinese `parameter_rows` from the same constants; the Dashboard renders only `parameter_rows`. Do not read `纪律.md` at runtime.

Add `lot_size: int` to each generated buy action and populate it from the same constants as the snapshot: CN is 100, US is 1, and HK is the exact per-symbol Futu lot size resolved when the report is generated. The open executor must reuse this frozen action value rather than querying or inventing a different lot size later.

Before publishing a report, call `validate_report_strategy_snapshot(report)`. It compares every formal buy action's target weight, lot size, buy window and initial protection multiple with `strategy_snapshot`; any mismatch raises `TrendStrategySnapshotMismatchError` and prevents publication. Add a regression test that deliberately changes an action weight while leaving the snapshot unchanged and assert the report is rejected.

Update `纪律.md` in the same commit so it states the exact current CN gates, temperature weights, sorting, position limit, buy window, initial `2 × ATR14` protection and five-day non-decreasing trailing line, plus the exact shared US/HK gates and their market-specific lot/window rules. It must not describe planned parameter export or backtesting as available now.

Add the three simulate account IDs to `DailyPremarketConfig`. `config/daily_premarket.env.example` contains the three exact keys with empty values; `load_env_config` accepts empty values for unrelated commands and validates populated IDs as distinct positive integers. Task 5 makes the selected market account mandatory for the live review workflow.

- [ ] **Step 4: Run focused trend tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_a_share_trend.py tests/test_market_trend.py \
  tests/test_daily_premarket.py -k 'trend or config'
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/a_share_trend.py src/open_trader/market_trend.py 纪律.md \
  src/open_trader/daily_premarket.py config/daily_premarket.env.example \
  tests/test_a_share_trend.py tests/test_market_trend.py tests/test_daily_premarket.py
git commit -m "feat: freeze trend strategy parameters"
```

---

### Task 2: 冻结可重放证据并保留纠正产物

**Files:**
- Create: `src/open_trader/trend_review.py`
- Create: `tests/test_trend_review.py`
- Modify: `src/open_trader/a_share_trend.py`
- Modify: `src/open_trader/market_trend.py`

**Interfaces:**
- Consumes: live report input rows before mapping, account snapshot, K-line rows, query field lists, report payload and process SHA.
- Produces: `freeze_trend_evidence(data_dir: Path, evidence: Mapping[str, object]) -> dict[str, str]`, `rebuild_trend_report_from_evidence(evidence: Mapping[str, object]) -> dict[str, object]`, `replay_trend_evidence(evidence_path: Path, data_dir: Path, fixed_process_version: str, rebuild: Callable) -> Path`, immutable evidence under `data/trend_review/evidence/CN|US|HK/`, and corrected artifacts under `data/trend_review/replays/CN|US|HK/`.

- [ ] **Step 1: Write failing immutability and replay tests**

```python
def test_freeze_and_replay_never_overwrite_original(tmp_path: Path) -> None:
    evidence = {
        "market": "CN",
        "report_id": "2026-07-16",
        "query": {"component_pool_ids": [622466, 697199], "snapshot_fields": ["tmId"]},
        "responses": {"components": [{"tmId": 1}], "snapshots": [{"tmId": 1}]},
        "market_data": {"SH.600001": [{"date": "2026-07-16", "close": "10"}]},
        "account": {"net_value": "100000"},
        "strategy_snapshot": {"strategy_version": "v1"},
        "process_version": "oldsha",
    }
    reference = freeze_trend_evidence(tmp_path, evidence)
    original = Path(reference["path"]).read_bytes()

    corrected = replay_trend_evidence(
        Path(reference["path"]), tmp_path,
        fixed_process_version="newsha",
        rebuild=lambda frozen: {"status": "corrected", "source": frozen["report_id"]},
        replayed_at="2026-07-17T09:00:00+08:00",
    )

    assert Path(reference["path"]).read_bytes() == original
    assert json.loads(corrected.read_text())["original_evidence_sha256"] == reference["sha256"]


def test_replay_marks_missing_original_input_instead_of_guessing(tmp_path: Path) -> None:
    # Freeze evidence whose original query omitted the required field.
    reference = freeze_trend_evidence(tmp_path, incomplete_evidence())
    with pytest.raises(TrendReplayIncompleteError, match="missing original input"):
        replay_trend_evidence(
            Path(reference["path"]), tmp_path,
            fixed_process_version="newsha",
            rebuild=require_missing_field,
        )
```

- [ ] **Step 2: Run replay tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_trend_review.py -k 'freeze or replay'
```

Expected: FAIL because `trend_review.py` does not exist.

- [ ] **Step 3: Implement one immutable JSON writer and replay envelope**

Use content hashes as identities; a second write of identical evidence returns the same file, while different bytes never replace it:

```python
EVIDENCE_SCHEMA_VERSION = "open_trader.trend_review.evidence.v1"
REPLAY_SCHEMA_VERSION = "open_trader.trend_review.replay.v1"


def freeze_trend_evidence(data_dir: Path, evidence: Mapping[str, object]) -> dict[str, str]:
    payload = {"schema_version": EVIDENCE_SCHEMA_VERSION, **dict(evidence)}
    body = _canonical_json_bytes(payload)
    digest = hashlib.sha256(body).hexdigest()
    market = _market(payload["market"])
    path = data_dir / "trend_review" / "evidence" / market / f"{digest}.json"
    _write_immutable(path, body)
    return {"path": str(path), "sha256": digest}


def replay_trend_evidence(
    evidence_path: Path,
    data_dir: Path,
    *,
    fixed_process_version: str,
    rebuild: Callable[[dict[str, object]], dict[str, object]],
    replayed_at: str | None = None,
) -> Path:
    original = _load_valid_evidence(evidence_path)
    corrected = rebuild(copy.deepcopy(original))
    payload = {
        "schema_version": REPLAY_SCHEMA_VERSION,
        "market": original["market"],
        "original_evidence_path": str(evidence_path),
        "original_evidence_sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
        "fixed_process_version": fixed_process_version,
        "replayed_at": replayed_at or _now(),
        "corrected_report": corrected,
    }
    return _write_unique_replay(data_dir, payload)
```

`_write_immutable` must use `os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)` and compare existing bytes on collision. It must never call `replace` on an existing evidence or replay file.

- [ ] **Step 4: Freeze live source inputs before report publication**

Both report generators pass the exact query requests and returned rows, account, prior state, watch events, all daily bars, strategy snapshot, fees, and process SHA to `freeze_trend_evidence`. Add only this reference to the report JSON:

```python
"replay_evidence": {
    "path": "trend_review/evidence/CN/7b8d7f6a00000000000000000000000000000000000000000000000000000000.json",
    "sha256": "7b8d7f6a00000000000000000000000000000000000000000000000000000000",
}
```

The report must not embed the large raw evidence. Add a test that removing an originally requested response field causes replay to report incomplete input, not call Trend Animals again.

Implement `rebuild_trend_report_from_evidence` by reconstructing the existing `AccountSnapshot`, component/snapshot joins, `CandidateInput`, `HoldingSnapshot`, K-line rows, prior protection state and watch events solely from the frozen payload, then calling the existing `evaluate_candidate` and `build_report`. It must never instantiate `TrendAnimalsClient` or `FutuQuoteClient`. The corrected report receives the fixed process SHA and a reference to the original evidence; missing original fields raise `TrendReplayIncompleteError`.

- [ ] **Step 5: Run focused tests and commit**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_trend_review.py \
  tests/test_a_share_trend.py -k 'evidence or report_payload' \
  tests/test_market_trend.py -k 'evidence or report'
```

Expected: PASS.

```bash
git add src/open_trader/trend_review.py src/open_trader/a_share_trend.py \
  src/open_trader/market_trend.py tests/test_trend_review.py \
  tests/test_a_share_trend.py tests/test_market_trend.py
git commit -m "feat: freeze replayable trend evidence"
```

---

### Task 3: 提交纪律模拟市价单并冻结日终事实

**Files:**
- Modify: `src/open_trader/kelly_order_execution.py`
- Modify: `tests/test_kelly_order_execution.py`
- Modify: `src/open_trader/trend_review.py`
- Modify: `tests/test_trend_review.py`

**Interfaces:**
- Consumes: frozen trend report, one market-specific `simulate_acc_id` and existing Futu trade context.
- Produces: `execute_trend_review_open(data_dir, report, client, prices, market, execution_date, now) -> dict[str, object]`, `execute_trend_review_stop(data_dir, market, symbol, trading_date, event_id, client, now) -> dict[str, object]`, `capture_trend_review_close(data_dir, market, trading_date, report, simulate_snapshot, orders, benchmark) -> Path`, daily facts such as `data/trend_review/daily/CN/2026-07-17.json`.

- [ ] **Step 1: Write failing Futu market-order compatibility tests**

```python
def test_simulate_client_submits_market_order_without_changing_limit_default() -> None:
    context = FakeTradeContext()
    client = FutuSimulateOrderExecutionClient(
        host="127.0.0.1", port=11111, simulate_acc_id=101,
        trd_market="CN", context_factory=lambda **_: context,
        connectivity_checker=lambda *_: True,
    )
    client.place_order({
        "side": "buy", "price": "0", "qty": "100", "futu_code": "SH.600001",
        "order_type": "MARKET", "remark": "trend:CN:2026-07-17:1",
    })
    assert context.place_calls[0]["order_type"] == "MARKET"
    assert context.place_calls[0]["price"] == 0.0
```

Also assert the existing Kelly request without `order_type` still sends `order_type="NORMAL"` and its supplied limit price.

- [ ] **Step 2: Write failing sizing, idempotence and fee tests**

```python
def test_open_uses_sim_nav_target_weight_and_cn_lot(tmp_path: Path) -> None:
    client = FakeTrendSimClient(nav="100000", positions=[])
    result = execute_trend_review_open(
        data_dir=tmp_path, report=cn_buy_report(weight="0.04", close="10"),
        client=client, prices={"600001": Decimal("10")},
        market="CN", execution_date="2026-07-17",
        now="2026-07-17T09:31:00+08:00",
    )
    assert client.requests[0]["qty"] == "400"
    assert client.requests[0]["order_type"] == "MARKET"
    assert result["submitted_count"] == 1
    assert execute_trend_review_open(
        data_dir=tmp_path, report=cn_buy_report(weight="0.04", close="10"),
        client=client, prices={"600001": Decimal("10")},
        market="CN", execution_date="2026-07-17",
        now="2026-07-17T09:32:00+08:00",
    )["submitted_count"] == 0


def test_first_open_requires_an_empty_dedicated_simulate_account(tmp_path: Path) -> None:
    client = FakeTrendSimClient(
        nav="100000", positions=[{"code": "SH.600001", "qty": "100"}]
    )
    with pytest.raises(
        TrendReviewAccountStateError,
        match="simulate account must start with zero positions",
    ):
        execute_trend_review_open(
            data_dir=tmp_path, report=cn_buy_report(weight="0.04", close="9"),
            client=client, prices={"600001": Decimal("10")},
            market="CN", execution_date="2026-07-17",
            now="2026-07-17T09:31:00+08:00",
        )


def test_close_uses_authoritative_simulate_account_nav(tmp_path: Path) -> None:
    path = capture_trend_review_close(
        data_dir=tmp_path, market="CN", trading_date="2026-07-17",
        report=cn_report(account_net_value="735164.41"),
        simulate_snapshot=sim_snapshot(nav="101000"),
        orders=[filled_buy(notional="4000"), filled_sell(notional="4200")],
        benchmark={"date": "2026-07-17", "close": "6123.45", "source_id": "CSI_ALL_SHARE_PRICE", "futu_symbol": "SH.000985"},
    )
    payload = json.loads(path.read_text())
    assert payload["discipline_equity_after_fees"] == "101000.00"
    assert payload["actual_equity"] == "735164.41"
```

- [ ] **Step 3: Run tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_kelly_order_execution.py -k 'market_order or default_limit' \
  tests/test_trend_review.py -k 'open or close or fee'
```

Expected: FAIL because the existing client hardcodes `NORMAL` and no review executor exists.

- [ ] **Step 4: Extend the existing simulate client minimally**

Change only the order-type selection and add read methods that use the already selected account:

```python
order_type = str(request.get("order_type") or "NORMAL").upper()
price = 0.0 if order_type == "MARKET" else float(request["price"])
self.context.place_order(
    price=price,
    qty=float(request["qty"]),
    code=request["futu_code"],
    trd_side=trd_side,
    order_type=order_type,
    trd_env=TRD_ENV_SIMULATE,
    acc_id=self.account["acc_id"],
    acc_index=self.account["acc_index"],
    remark=request.get("remark") or None,
)
```

Add `account_snapshot()` using `accinfo_query` plus `position_list_query`, and `list_orders()` using `order_list_query`. Return raw records plus `acc_id`; do not create another Futu client class.

- [ ] **Step 5: Implement open, stop and close functions with immutable ledgers**

Required public call shapes:

```python
open_result = execute_trend_review_open(
    data_dir=data_dir, report=report, client=client, prices=current_open_quotes,
    market="CN", execution_date="2026-07-17", now="2026-07-17T09:31:00+08:00",
)
stop_result = execute_trend_review_stop(
    data_dir=data_dir, market="CN", symbol="600001", trading_date="2026-07-17",
    event_id="event-1", client=client, now="2026-07-17T10:15:00+08:00",
)
daily_path = capture_trend_review_close(
    data_dir=data_dir, market="CN", trading_date="2026-07-17",
    report=report, simulate_snapshot=simulate_snapshot, orders=orders,
    benchmark=benchmark,
)
```

On the first ledger day, require the dedicated simulate account to have zero positions; otherwise fail before placing any order. Later days reconcile positions against this experiment's immutable order ledger and reject unexplained holdings. Size every market order from the simulate account's current NAV, the frozen target weight, and the current opening quote in `prices`; use the report action's frozen lot size for rounding. Never size from the previous report close.

Use one immutable ledger key per `(market, execution_date, report_sha256, action_index)` and per protection `event_id`. If the valid A/HK window has passed, record `missed_window`; never submit late. If the simulate account is unavailable, leave the previous curve unchanged and write a failure fact; never pretend the account remained in cash.

- [ ] **Step 6: Run focused tests and commit**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_kelly_order_execution.py tests/test_trend_review.py
```

Expected: PASS.

```bash
git add src/open_trader/kelly_order_execution.py src/open_trader/trend_review.py \
  tests/test_kelly_order_execution.py tests/test_trend_review.py
git commit -m "feat: execute trend discipline simulation"
```

---

### Task 4: 生成 30 笔批次、三条曲线和五项指标

**Files:**
- Modify: `src/open_trader/tiger_long_term_backtest.py`
- Modify: `tests/test_tiger_long_term_backtest.py`
- Modify: `src/open_trader/trend_review.py`
- Modify: `tests/test_trend_review.py`

**Interfaces:**
- Consumes: daily review facts containing validated frozen Futu benchmark closes, `data/rates/DGS3MO.csv` and completed simulated trades.
- Produces: public `portfolio_metrics(curve, rates, initial_cash) -> dict[str, object]`, `build_trend_review_projection(data_dir: Path, market: str) -> dict[str, object]`, immutable batch files such as `data/trend_review/batches/CN/0001.json`, and atomic latest files `data/latest/trend_review_cn.json`, `trend_review_us.json`, `trend_review_hk.json`.

- [ ] **Step 1: Write failing public metric tests**

```python
def test_portfolio_metrics_uses_risk_free_excess_returns() -> None:
    curve = [
        {"date": "2026-07-01", "equity": "100"},
        {"date": "2026-07-02", "equity": "101"},
        {"date": "2026-07-03", "equity": "100"},
    ]
    metrics = portfolio_metrics(curve, {date(2026, 7, 1): Decimal("4")}, Decimal("100"))
    assert set(metrics) == {
        "total_return_pct", "annualized_return_pct", "max_drawdown_pct",
        "sharpe_ratio", "calmar_ratio",
    }
    assert metrics["sharpe_ratio"] is not None
```

Rename `_portfolio_metrics` to `portfolio_metrics` and update only internal callers. Keep formula behavior unchanged.

- [ ] **Step 2: Write failing batch and projection tests**

```python
def test_projection_closes_non_overlapping_batch_at_thirtieth_trade(tmp_path: Path) -> None:
    write_daily_facts(tmp_path, completed_trades=31, days=45)
    projection = build_trend_review_projection(tmp_path, "CN")
    assert projection["batch"]["completed_trade_count"] == 30
    assert projection["batch"]["batch_number"] == 1
    assert Path(projection["batch_path"]).exists()
    assert projection["metrics"].keys() == {
        "period_net_return", "market_excess_return", "max_drawdown",
        "calmar", "sharpe",
    }
    assert all(set(values) == {"discipline", "actual", "benchmark"}
               for values in projection["metrics"].values())


def test_projection_marks_missing_actual_curve_as_data_insufficient(tmp_path: Path) -> None:
    write_daily_facts(tmp_path, completed_trades=30, days=40, missing_actual_date=True)
    projection = build_trend_review_projection(tmp_path, "CN")
    assert projection["metrics"]["sharpe"]["actual"] == {
        "value": None, "reason": "实际执行日终净值缺失"
    }
```

Also test: 29 trades never create an immutable batch; trade 31 starts batch 2; a symbol with partial exits counts only once when its experiment position returns to zero; versions are attributed by entry snapshot; zero drawdown/zero volatility yield `value=None`; missing benchmark blocks publishing all five comparisons; benchmark excess is exactly zero.

- [ ] **Step 3: Run metrics tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_tiger_long_term_backtest.py -k portfolio_metrics \
  tests/test_trend_review.py -k 'projection or batch or metric'
```

Expected: FAIL because `portfolio_metrics` is private and no batch builder exists.

- [ ] **Step 4: Validate benchmark identity and build aligned curves**

Add the exact map:

```python
BENCHMARK_SOURCE_IDS = {
    "CN": "CSI_ALL_SHARE_PRICE",
    "US": "SPY_QFQ",
    "HK": "HSCI_PRICE",
}
```

`benchmark_fact` fetches the exact review date from Futu and rejects missing, non-finite/non-positive closes. Each daily fact validates date, source ID and Futu symbol. `build_trend_review_projection` intersects all three curves on the same dates, normalizes each to its first value, and passes each normalized curve to `portfolio_metrics`. It writes these exact five keys:

```python
metrics = {
    "period_net_return": values("total_return_pct"),
    "market_excess_return": {
        "discipline": difference(discipline, benchmark),
        "actual": difference(actual, benchmark),
        "benchmark": metric_value("0"),
    },
    "max_drawdown": values("max_drawdown_pct"),
    "calmar": values("calmar_ratio"),
    "sharpe": values("sharpe_ratio"),
}
```

Do not copy annualized return into the Dashboard payload; it is only an internal input to Calmar.

- [ ] **Step 5: Write immutable batch and latest projection atomically**

Build completed trades from the immutable experiment ledger with FIFO fill matching. One trade begins when a symbol moves from zero to a positive experiment quantity and completes only when all quantities from that entry are closed; partial exits remain inside that same trade. Assign it the entry action's `strategy_snapshot` and never move it to a later version or batch.

The immutable batch contains the complete `strategy_snapshot`, fee/rate/benchmark hashes, three daily curves, five metrics, the 30 completed trade references, generation time and Git SHA. The latest projection may represent 0–29 trades and must use `value=None` reasons; it is the only mutable file and is replaced atomically.

- [ ] **Step 6: Run tests and commit**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_tiger_long_term_backtest.py tests/test_trend_review.py
```

Expected: PASS.

```bash
git add src/open_trader/tiger_long_term_backtest.py src/open_trader/trend_review.py \
  tests/test_tiger_long_term_backtest.py tests/test_trend_review.py
git commit -m "feat: build trend review batches and metrics"
```

---

### Task 5: 接入配置、CLI、报告日终和现有 watcher

**Files:**
- Modify: `src/open_trader/daily_premarket.py`
- Modify: `src/open_trader/a_share_trend_watch.py`
- Modify: `src/open_trader/market_trend_watch.py`
- Modify: `src/open_trader/cli.py`
- Test: `tests/test_daily_premarket.py`
- Test: `tests/test_a_share_trend_watch.py`
- Test: `tests/test_market_trend_watch.py`
- Test: `tests/test_premarket_cli.py`

**Interfaces:**
- Consumes: Task 3 open/stop/close functions and Task 4 projection builder.
- Produces: config fields `trend_review_simulate_acc_ids`, `trend_review_cost_bps`; CLI `trend-review open|close|replay`; automatic calls from existing report and watcher jobs without new launchd agents.

- [ ] **Step 1: Write failing selected-market configuration tests**

Using the nine fields added in Task 1, test `require_trend_review_config(config, market)` directly. Assert it returns the selected positive account ID and calibrated buy/sell bps, rejects a missing selected-market value, rejects non-finite or negative bps, and rejects reuse of any configured account ID across markets. Unrelated commands still load when all nine example values are empty; `trend-review` and live trend watcher/report integration fail closed before external calls when the selected market is incomplete.

- [ ] **Step 2: Write failing watcher hook tests**

```python
def test_cn_watcher_executes_open_once_and_stop_once() -> None:
    opens, stops = [], []
    result = watch_a_share_protection(
        # existing test dependencies
        on_session_open=lambda trading_date: opens.append(trading_date),
        on_protection_trigger=lambda event: stops.append(event["event_id"]),
        once=True,
    )
    assert opens == ["2026-07-17"]
    assert len(stops) == 1
```

For HK/US, assert `watch_market_protection` sleeps to the correct market open before invoking the same hooks. A/HK open execution after 10:00 records `missed_window`; US allows its regular session. Replayed watcher events must not resubmit a stop with the same `event_id`.

- [ ] **Step 3: Write failing CLI tests**

Test these direct workflows with injected fakes:

```bash
.venv/bin/python -m open_trader trend-review open --market CN --date 2026-07-17 --config config/daily_premarket.env
.venv/bin/python -m open_trader trend-review close --market CN --date 2026-07-17 --config config/daily_premarket.env
.venv/bin/python -m open_trader trend-review replay --evidence data/trend_review/evidence/CN/7b8d7f6a00000000000000000000000000000000000000000000000000000000.json --config config/daily_premarket.env
```

Expected JSON fields are `status`, `market`, `date`, `artifact_path`; errors return exit 1 and never print account IDs or API keys.

- [ ] **Step 4: Run tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_daily_premarket.py -k trend_review \
  tests/test_a_share_trend_watch.py -k 'session_open or protection_trigger' \
  tests/test_market_trend_watch.py -k 'session_open or protection_trigger' \
  tests/test_premarket_cli.py -k trend_review
```

Expected: FAIL because config fields, hooks and CLI do not exist.

- [ ] **Step 5: Add the two optional watcher callbacks and CLI wiring**

Add only these optional parameters to the shared watcher:

```python
on_session_open: Callable[[str], None] | None = None,
on_protection_trigger: Callable[[Mapping[str, object]], None] | None = None,
```

Call `on_session_open(trading_date)` exactly once after the trading-calendar check and before polling prices. Call `on_protection_trigger(event)` only after the immutable `protection_triggered` event is appended. Existing callers pass nothing and remain unchanged.

The existing `trend-a-share-report` and `trend-market-report` commands call `capture_trend_review_close` after a complete frozen report exists. Existing watcher commands construct one simulate client for the selected account and pass the two callbacks. Do not create new launchd files: current report and watcher jobs already cover market close and next open.

- [ ] **Step 6: Run direct fake workflows and focused tests**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_daily_premarket.py tests/test_a_share_trend_watch.py \
  tests/test_market_trend_watch.py tests/test_premarket_cli.py
```

Expected: PASS.

Run a deterministic local direct check using a temporary config and fake client through the tested Python entry point; confirm the second `open` call reports zero submissions and the original daily artifact bytes do not change.

- [ ] **Step 7: Commit**

```bash
git add src/open_trader/daily_premarket.py \
  src/open_trader/a_share_trend_watch.py src/open_trader/market_trend_watch.py \
  src/open_trader/cli.py tests/test_daily_premarket.py \
  tests/test_a_share_trend_watch.py tests/test_market_trend_watch.py \
  tests/test_premarket_cli.py
git commit -m "feat: automate trend review collection"
```

---

### Task 6: 将复盘产物投影到 Dashboard

**Files:**
- Modify: `src/open_trader/dashboard.py:100-280,296-470`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `data/latest/trend_review_cn.json`, `trend_review_us.json`, `trend_review_hk.json`.
- Produces: `DashboardState.trend_reviews: dict[str, dict[str, Any]]` keyed by `eastmoney|futu|phillips`.

- [ ] **Step 1: Write failing projection tests**

```python
def test_dashboard_loads_only_matching_market_review(tmp_path: Path) -> None:
    write_review(tmp_path, "CN", broker="eastmoney")
    write_review(tmp_path, "US", broker="futu")
    write_review(tmp_path, "HK", broker="phillips")
    state = load_dashboard_state(config(tmp_path)).to_dict()
    assert state["trend_reviews"]["eastmoney"]["market"] == "CN"
    assert state["trend_reviews"]["futu"]["market"] == "US"
    assert state["trend_reviews"]["phillips"]["market"] == "HK"
    assert "tiger" not in state["trend_reviews"]


def test_dashboard_rejects_review_with_extra_metric(tmp_path: Path) -> None:
    write_review(tmp_path, "CN", extra_metric={"beta": {}})
    review = load_dashboard_state(config(tmp_path)).to_dict()["trend_reviews"]["eastmoney"]
    assert review == {"available": False, "status_text": "复盘数据无效"}
```

Also reject wrong broker/market, malformed `strategy_snapshot`, non-finite values, wrong metric keys, wrong comparison keys, and arrays where objects are required. Missing files return `available=False` without breaking holdings or trend reports.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_dashboard.py -k trend_review
```

Expected: FAIL because `DashboardState` has no `trend_reviews`.

- [ ] **Step 3: Add one strict loader and state field**

Use fixed configuration beside `TREND_REPORT_SOURCES`:

```python
TREND_REVIEW_SOURCES = {
    "futu": ("US", "美股", "trend_review_us.json"),
    "phillips": ("HK", "港股", "trend_review_hk.json"),
    "eastmoney": ("CN", "A股", "trend_review_cn.json"),
}
TREND_REVIEW_METRICS = {
    "period_net_return", "market_excess_return", "max_drawdown", "calmar", "sharpe"
}
TREND_REVIEW_SERIES = {"discipline", "actual", "benchmark"}
```

The loader passes through only validated values. It does not calculate metrics, read evidence, or inspect historical batches during a Dashboard request.

- [ ] **Step 4: Run tests and commit**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_dashboard.py
```

Expected: PASS.

```bash
git add src/open_trader/dashboard.py tests/test_dashboard.py
git commit -m "feat: project trend reviews to dashboard"
```

---

### Task 7: 账户内入口、完整参数表和两组原生图表

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js:40-360,860-910,1890-2450`
- Modify: `src/open_trader/dashboard_static/dashboard.css:1380-1770,3960-4140`
- Test: `tests/test_dashboard_web.py`
- Test: `tests/e2e/dashboard-warm-ledger.spec.ts`

**Interfaces:**
- Consumes: `state.dashboard.trend_reviews[broker]`.
- Produces: `renderTrendReviewEntry(broker)`, `openTrendReview(broker)`, `renderTrendReviewWorkspace(review)`, rendered into the existing `trend-report-workspace`.

- [ ] **Step 1: Write failing entry and exact-content tests**

Add a JS harness test that renders all four accounts and asserts:

```javascript
for (const [broker, label] of [
  ["eastmoney", "A 股复盘"], ["futu", "美股复盘"], ["phillips", "港股复盘"],
]) {
  const account = renderAccountSection(group(broker));
  if (!account.includes(`data-trend-review="${broker}"`) || !account.includes(label)) {
    throw new Error(account);
  }
}
if (renderAccountSection(group("tiger")).includes("data-trend-review")) throw new Error("tiger entry");
```

Open A 股 review and assert the workspace contains the strategy name/version, every parameter label/value, and exactly these metric labels:

```javascript
const metrics = ["期间净收益率", "相对市场超额收益", "最大回撤", "卡玛比率", "夏普比率"];
for (const label of metrics) if (!html.includes(label)) throw new Error(label);
for (const forbidden of ["复盘结论", "运行状态", "创建回测", "导出参数", "Connected", "Alpha", "Beta", "Sortino", "胜率", "盈亏比"]) {
  if (html.includes(forbidden)) throw new Error(forbidden);
}
```

Assert all untrusted strategy and metric strings pass through `escapeHtml` and all missing metric values render `数据不足`.

- [ ] **Step 2: Write failing mobile and E2E tests**

At 375px, click 东方财富 then `A 股复盘`; assert `document.documentElement.scrollWidth === 375`, every parameter value is visible, and the two chart groups are present. Return to holdings and assert focus returns to the same `A 股复盘` button.

- [ ] **Step 3: Run frontend tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_dashboard_web.py -k trend_review
npx playwright test tests/e2e/dashboard-warm-ledger.spec.ts -g "trend review"
```

Expected: FAIL because the entry and renderer do not exist.

- [ ] **Step 4: Reuse the existing workspace and warm outline style**

In `renderAccountSection`, render the buttons next to each other inside the existing `.trend-report-entry`:

```javascript
<div class="trend-report-entry">
  ${renderTrendReportEntry(group.broker)}
  ${renderTrendReviewEntry(group.broker)}
</div>
```

`openTrendReview` stores the selected broker and source kind, writes `renderTrendReviewWorkspace(review)` into `trend-report-workspace`, and calls the existing `setWorkspaceView("trend_report")`. Do not add another workspace element or global header button.

- [ ] **Step 5: Render only the approved structures**

Render one compact header containing only the broker label, market label, `strategy_name` and `strategy_version`. Then use `review.strategy_snapshot.parameter_rows` as the only parameter-table source: one semantic table on desktop and CSS grid rows on mobile. Never display machine keys from `parameters`. Render exactly two charts after the table:

```javascript
renderTrendReviewChart("收益与回撤", [
  ["期间净收益率", "period_net_return"],
  ["相对市场超额收益", "market_excess_return"],
  ["最大回撤", "max_drawdown"],
], review.metrics)

renderTrendReviewChart("风险调整收益", [
  ["卡玛比率", "calmar"],
  ["夏普比率", "sharpe"],
], review.metrics)
```

Each group uses three native CSS bars labelled `纪律模拟`、`实际执行`、`市场基准`; compute relative width from the maximum absolute finite value in that chart and print the exact value beside the bar. Color is never the only distinction. Do not add a chart library.

- [ ] **Step 6: Run frontend tests and commit**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_dashboard_web.py
npx playwright test tests/e2e/dashboard-warm-ledger.spec.ts
```

Expected: PASS.

```bash
git add src/open_trader/dashboard_static/dashboard.js \
  src/open_trader/dashboard_static/dashboard.css tests/test_dashboard_web.py \
  tests/e2e/dashboard-warm-ledger.spec.ts
git commit -m "feat: show trend strategy reviews"
```

---

### Task 8: Acceptance、真实流程和同 SHA 部署

**Files:**
- Modify: `src/open_trader/dashboard_acceptance.py`
- Modify: `tests/test_dashboard_acceptance.py`

**Interfaces:**
- Consumes: complete Dashboard behavior from Tasks 1–7 and live configured services.
- Produces: final `make acceptance` result and deployed Review URL.

- [ ] **Step 1: Write failing acceptance-helper tests**

Extend the acceptance fake page to verify:

```python
for broker, label in (
    ("eastmoney", "A 股复盘"), ("futu", "美股复盘"), ("phillips", "港股复盘")
):
    page.locator(f'#account-{broker}:visible [data-trend-review="{broker}"]').click()
    assert page.locator("#trend-report-workspace:visible").contains_text(label.replace("复盘", "趋势复盘"))
    assert page.locator("#trend-report-workspace:visible .trend-review-parameter-table").is_visible()
    assert page.locator("#trend-report-workspace:visible .trend-review-chart").count() == 2
```

Also assert Tiger has no entry, all five metric labels appear once, forbidden content is absent, and the 375px page has no horizontal overflow.

- [ ] **Step 2: Run acceptance-helper tests and verify RED, then implement**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_dashboard_acceptance.py -k trend_review
```

Expected before implementation: FAIL. Add only the new selectors and checks, rerun, and expect PASS.

- [ ] **Step 3: Run all focused automated tests**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_trend_review.py tests/test_a_share_trend.py tests/test_market_trend.py \
  tests/test_kelly_order_execution.py tests/test_tiger_long_term_backtest.py \
  tests/test_daily_premarket.py tests/test_a_share_trend_watch.py \
  tests/test_market_trend_watch.py tests/test_premarket_cli.py \
  tests/test_dashboard.py tests/test_dashboard_web.py \
  tests/test_dashboard_acceptance.py
```

Expected: PASS with the exact test count printed by pytest.

- [ ] **Step 4: Run the real review workflow directly**

With the three distinct simulate account IDs configured, run each market's direct close workflow. The close command freezes each market's Futu benchmark fact and does not submit duplicate open orders:

```bash
PYTHONPATH=src .venv/bin/python -m open_trader trend-review close --market CN --date today --config config/daily_premarket.env
PYTHONPATH=src .venv/bin/python -m open_trader trend-review close --market US --date today --config config/daily_premarket.env
PYTHONPATH=src .venv/bin/python -m open_trader trend-review close --market HK --date today --config config/daily_premarket.env
```

Confirm each output references the correct account, market, benchmark source ID, strategy version and process SHA. Confirm missing samples display `数据不足` and no output contains non-finite numbers. Do not place a live simulated order outside its valid window merely to satisfy verification.

- [ ] **Step 5: Inspect and restart long-running processes**

Inspect `screen -ls`, `launchctl list | rg 'open-trader|trend'`, listener PIDs, cwd and command lines. Reinstall/restart existing trend report/watch jobs so none retain pre-change code. Verify fresh logs include the new PID, timestamp, Git SHA and review close/open event where applicable.

- [ ] **Step 6: Commit acceptance changes**

```bash
git add src/open_trader/dashboard_acceptance.py tests/test_dashboard_acceptance.py
git commit -m "test: accept trend strategy reviews"
```

- [ ] **Step 7: Run the final gate once**

Run:

```bash
make acceptance
```

Expected: final JSON status `PASS`. On `FAIL`, diagnose and fix, rerun focused checks, then rerun `make acceptance`. On `BLOCKED`, report the exact unavailable browser/external dependency and do not substitute fixtures, curl or screenshots.

- [ ] **Step 8: Redeploy the exact accepted SHA and hand off**

Record `git rev-parse HEAD`, restart the Dashboard without source/data changes, then verify:

```bash
lsof -nP -iTCP:8766 -sTCP:LISTEN
curl -I http://127.0.0.1:8766/
```

Confirm the new PID cwd is `/Users/ray/projects/open_trader`, deployed Git SHA equals the accepted SHA, fresh logs are from the new process, and the Review URL returns HTTP 200. Only then provide the URL to the user.
