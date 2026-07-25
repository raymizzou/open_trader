# 三市场趋势退出纪律 v9/v6 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 发布 CN v9、US v6、HK v6：三市场新版本取消过热减仓，保留危险、离开右侧、温度转平和 `2 × ATR14` 全部清仓，A 股热/沸新仓统一为账户净值 4%。

**Architecture:** 继续由 `a_share_trend.py` 生成唯一冻结策略快照和交易决定；新版本通过现有快照版本分支启用新纪律，旧版本回放仍走原 30% 部分卖出路径。现有 Dashboard、复盘和 acceptance 只扩展版本白名单并消费冻结字段，不复制交易规则；回撤 preflight 从明确批准的前一版本复制高水位和暂停状态。

**Tech Stack:** Python 3.12、标准库 `dataclasses`/`decimal`/`json`/`pathlib`、pytest、原生 JavaScript、Playwright、现有 launchd/screen 运维脚本。

## Global Constraints

- 工作目录固定为 `/Users/ray/projects/open_trader/.worktrees/trend-exit-discipline-v9`，分支固定为 `feat/trend-exit-discipline-v9`，基线为本地 `main` 的 `75a29cff00873d158ac8e2ad7ed5e5575aa013f0`。
- 不新增依赖、配置层、策略类或通用迁移框架。
- CN v9 的“热”和“沸”目标仓位都为 `0.04`；CN v8 及更早冻结版本继续保留“沸 `0.02`”。
- CN v9、US v6、HK v6 不生成 `SELL_PARTIAL`，不因 boiling/champagne 上移保护线。
- 新版本的全部清仓条件只有既有四项：保护线、危险信号、离开右侧、温/热/沸转平；同批优先级保持保护线、危险、离开右侧、转平。
- 旧版本 `SELL_PARTIAL` 的回放、展示、执行、恢复和对账必须继续工作。
- 新持仓保护线为“合并买入成交均价 − `2 × ATR14`”；旧持仓已上移的活动保护线不得下调，新版本不再继续上移。
- Kelly 样本明确继承 CN `v4/v7/v8/v9`、US `v4/v5/v6`、HK `v4/v5/v6`；不得递归推导或跨市场继承。
- 回撤高水位和暂停状态从 CN v8、US v5、HK v5 接续，不以当前净值重新归零。
- 每个行为改动先写失败测试，再写最小实现；每个任务独立提交。
- `CHANGELOG.md` 必须在任何合并前提交。
- `make acceptance` 只能在全部源码和文档提交后作为最后门禁运行；只有 `PASS` 才能宣称完成。
- acceptance `PASS` 后必须重启完全相同的已验收 SHA，并验证 PID、工作目录、Git SHA、日志和 HTTP 200。

## File Map

- `src/open_trader/a_share_trend.py`：版本快照、A 股目标仓位、持仓动作、冻结 JSON/Markdown/飞书和报告校验的唯一事实源。
- `src/open_trader/trend_review.py`：冻结快照版本白名单、回放输入和跨批准版本复盘投影。
- `src/open_trader/dashboard.py`：Dashboard 后端对冻结风险/回撤契约的校验。
- `src/open_trader/dashboard_static/dashboard.js`：冻结纪律卡和动作原因的只读展示。
- `src/open_trader/dashboard_acceptance.py`：真实 Dashboard 验收允许的逐市场版本和页面断言。
- `src/open_trader/strategy_drawdown.py`：回撤记录的原子读取、校验和写入。
- `src/open_trader/drawdown_preflight.py`：部署前为三个新版本选择唯一批准的回撤前身。
- `tests/test_a_share_trend.py`、`tests/test_market_trend.py`：快照、仓位、退出、旧版回放和输出。
- `tests/test_trend_review.py`、`tests/test_dashboard.py`、`tests/test_dashboard_web.py`、`tests/test_dashboard_acceptance.py`：版本传播与冻结展示。
- `tests/test_strategy_drawdown.py`、`tests/test_drawdown_preflight.py`：回撤继承和失败关闭。
- `纪律.md`、`CONTEXT.md`、`README.md`、`CHANGELOG.md`：当前纪律、历史术语、旧动作兼容和操作日志。

---

### Task 1: 发布新策略快照并让 A 股仓位消费冻结版本

**Files:**
- Modify: `src/open_trader/a_share_trend.py:119-145`
- Modify: `src/open_trader/a_share_trend.py:417-756`
- Modify: `src/open_trader/a_share_trend.py:1930-2315`
- Modify: `src/open_trader/a_share_trend.py:2535-2965`
- Modify: `src/open_trader/a_share_trend.py:3800-3860`
- Modify: `src/open_trader/trend_review.py:80-82`
- Modify: `src/open_trader/trend_review.py:4360-4390`
- Test: `tests/test_a_share_trend.py:450-690`
- Test: `tests/test_a_share_trend.py:1680-1830`
- Test: `tests/test_market_trend.py:150-245`
- Test: `tests/test_trend_review.py:25-70`

**Interfaces:**
- Produces: `CURRENT_TREND_STRATEGY_VERSIONS: Mapping[str, str]` with `{"CN": "v9", "US": "v6", "HK": "v6"}`.
- Produces: `CURRENT_EXIT_DISCIPLINES: frozenset[tuple[str, str]]` for Task 2.
- Produces: `live_trend_strategy_snapshot(..., strategy_version="v9"|"v6")`.
- Produces: `_plan_buy_actions(..., cn_target_weights: Mapping[str, Decimal])`.
- Preserves: `trend_strategy_snapshot()` and explicitly requested old live versions return their original frozen weights and overheat parameters.

- [ ] **Step 1: Write failing snapshot and sizing tests**

Add tests that assert the current defaults, exact inheritance, removed parameters,
and old snapshot compatibility:

```python
@pytest.mark.parametrize(
    ("market", "version", "inherits"),
    [
        ("CN", "v9", ("v4", "v7", "v8", "v9")),
        ("US", "v6", ("v4", "v5", "v6")),
        ("HK", "v6", ("v4", "v5", "v6")),
    ],
)
def test_current_live_snapshots_publish_exit_discipline_without_partial_profit(
    market: str, version: str, inherits: tuple[str, ...],
) -> None:
    pools = (622466, 697199) if market == "CN" else (622460,) if market == "US" else (622494,)
    snapshot = trend_module.live_trend_strategy_snapshot(market, "abc123", pools)
    parameters = snapshot["parameters"]
    rows = {row["name"]: row["value"] for row in snapshot["parameter_rows"]}

    assert snapshot["strategy_version"] == version
    assert snapshot["strategy_id"] == f"trend_animals_warm_to_hot/{market}/{version}"
    assert parameters["kelly_sample_inherits"] == [
        {
            "market": market,
            "strategy_id": f"trend_animals_warm_to_hot/{market}/{item}",
            "opening_strategy_version": item,
        }
        for item in inherits
    ]
    assert parameters["exit_reasons"] == [
        "danger", "left_right_side", "temperature_to_flat", "protection",
    ]
    assert not any(key.startswith("overheat_trim_") for key in parameters)
    assert "full_exit_precedes_partial_exit" not in parameters
    assert "trailing_low_days" not in parameters
    assert not {
        "过热止盈比例", "过热止盈信号", "过热止盈次数", "过热止盈取整",
        "不足一手处理", "清仓优先级", "过热跟踪",
    } & rows.keys()
    if market == "CN":
        assert parameters["target_weight"] == {"热": "0.04", "沸": "0.04"}
        assert rows["目标仓位"] == "账户净值的 4%"
        assert "热状态仓位" not in rows
        assert "沸状态仓位" not in rows


def test_cn_v8_snapshot_and_sizing_keep_legacy_boiling_two_percent() -> None:
    snapshot = trend_module.live_trend_strategy_snapshot(
        "CN", "abc123", (622466, 697199), strategy_version="v8"
    )
    assert snapshot["parameters"]["target_weight"] == {"热": "0.04", "沸": "0.02"}
    assert snapshot["parameters"]["overheat_trim_fraction"] == "0.30"
    rows = {row["name"]: row["value"] for row in snapshot["parameter_rows"]}
    assert rows["沸状态仓位"] == "账户净值的 2%"


def test_current_cn_boiling_entry_uses_four_percent() -> None:
    actions = estimate_buy_actions(
        ranked=(candidate("600001", temperature_curr="沸"),),
        net_value=Decimal("100000"),
        available_cash=Decimal("100000"),
        current_position_count=0,
        position_weight=Decimal("0.04"),
    )
    assert actions[0].target_weight == Decimal("0.04")
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_a_share_trend.py \
  tests/test_market_trend.py \
  tests/test_trend_review.py \
  -q -k 'current_live_snapshots or cn_v8_snapshot or current_cn_boiling'
```

Expected: failures show CN defaults to v8, US/HK default to v5, boiling remains
`0.02`, and overheat parameters remain present.

- [ ] **Step 3: Add only the two required weight maps and version identities**

Use exact constants:

```python
LEGACY_CN_TARGET_WEIGHTS = {"热": Decimal("0.04"), "沸": Decimal("0.02")}
CN_TARGET_WEIGHTS = {"热": Decimal("0.04"), "沸": Decimal("0.04")}
CURRENT_TREND_STRATEGY_VERSIONS = {"CN": "v9", "US": "v6", "HK": "v6"}
CURRENT_TREND_EFFECTIVE_FROM = "2026-07-27"
CURRENT_EXIT_DISCIPLINES = frozenset(
    (market, version)
    for market, version in CURRENT_TREND_STRATEGY_VERSIONS.items()
)
OVERHEAT_PARAMETER_NAMES = frozenset({
    "overheat_trim_fraction",
    "overheat_trim_once_per_position",
    "overheat_trim_signals",
    "overheat_trim_rounding",
    "overheat_trim_below_lot",
    "full_exit_precedes_partial_exit",
    "trailing_low_days",
})
OVERHEAT_ROW_NAMES = frozenset({
    "过热止盈比例",
    "过热止盈信号",
    "过热止盈次数",
    "过热止盈取整",
    "不足一手处理",
    "清仓优先级",
    "过热跟踪",
})
```

Keep `trend_strategy_snapshot()` on `LEGACY_CN_TARGET_WEIGHTS`. In
`live_trend_strategy_snapshot()`:

```python
if strategy_version is not None:
    version = strategy_version
elif execution_date is None or execution_date >= CURRENT_TREND_EFFECTIVE_FROM:
    version = CURRENT_TREND_STRATEGY_VERSIONS[market]
elif market == "CN":
    version = "v8"
elif market in MARKET_V5_EFFECTIVE_FROM and execution_date >= MARKET_V5_EFFECTIVE_FROM[market]:
    version = "v5"
else:
    version = "v4"
```

Allow CN `v9`, US/HK `v6`, keep all old allowed combinations, and for current
versions apply:

```python
current_discipline = (market, version) in CURRENT_EXIT_DISCIPLINES
if current_discipline:
    for name in OVERHEAT_PARAMETER_NAMES:
        parameters.pop(name, None)
    parameters["exit_reasons"] = [
        "danger", "left_right_side", "temperature_to_flat", "protection",
    ]
    rows = [row for row in rows if row["name"] not in OVERHEAT_ROW_NAMES]
    if market == "CN":
        parameters["target_weight"] = {
            key: str(value) for key, value in CN_TARGET_WEIGHTS.items()
        }
        rows = [
            row for row in rows
            if row["name"] not in {"热状态仓位", "沸状态仓位"}
        ]
        buy_index = next(
            index for index, row in enumerate(rows)
            if row["name"] == "买入数量"
        )
        rows.insert(buy_index, {
            "group": "仓位执行",
            "name": "目标仓位",
            "value": "账户净值的 4%",
        })
    for row in rows:
        if row["name"] == "退出条件":
            row["value"] = "危险信号、离开趋势右侧、温度转平或触发保护线时全部卖出"
```

Set explicit inheritance tuples and `effective_from="2026-07-27"` for current
versions. Extend `_expected_report_strategy_snapshot()`,
`validate_report_strategy_snapshot()`, Kelly/risk/drawdown version checks and
their contract maps to accept CN v9 and US/HK v6 without changing old behavior.
Also add `v9` to `trend_review.TREND_STRATEGY_VERSIONS` and make
`normalize_trend_strategy_snapshot()` resolve v9/v6 through
`live_trend_strategy_snapshot()`; this is required before Task 2 can build a
report from a new snapshot.

- [ ] **Step 4: Make buy sizing consume the normalized frozen target map**

Add `cn_target_weights` to `_plan_buy_actions()` and replace its three direct
`CN_TARGET_WEIGHTS.get(...)` reads:

```python
def _plan_buy_actions(
    *,
    ranked: Sequence[CandidateInput],
    net_value: Decimal,
    available_cash: Decimal,
    current_position_count: int,
    position_weight: Decimal,
    market: str,
    lot_sizes: Mapping[str, int] | None,
    price_fx_to_account_currency: Decimal,
    portfolio_planned_risk: Decimal | None,
    normal_cost_rate: Decimal,
    cn_target_weights: Mapping[str, Decimal] = CN_TARGET_WEIGHTS,
    critical_data_reason: str = "",
    kelly_state: TrendKellyState | None = None,
) -> tuple[list[BuyAction], list[dict[str, object]], dict[str, object]]:
```

In `build_report()`, derive the two A-share weights from the already normalized
snapshot and fail closed if either is invalid:

```python
snapshot_parameters = resolved_strategy_snapshot.get("parameters")
raw_cn_weights = (
    snapshot_parameters.get("target_weight")
    if isinstance(snapshot_parameters, Mapping)
    else None
)
try:
    cn_target_weights = {
        key: Decimal(str(raw_cn_weights[key]))
        for key in ("热", "沸")
    } if market == "CN" and isinstance(raw_cn_weights, Mapping) else CN_TARGET_WEIGHTS
except (InvalidOperation, KeyError, ValueError):
    raise ValueError("strategy snapshot has invalid CN target weights") from None
```

Pass `cn_target_weights` to `_plan_buy_actions()` and use it when building
drawdown risk skips. Keep `_estimate_buy_actions_v1()` pinned to
`LEGACY_CN_TARGET_WEIGHTS` so v1 evidence replay cannot change.

- [ ] **Step 5: Run snapshot, sizing and legacy replay tests**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_a_share_trend.py \
  tests/test_market_trend.py \
  tests/test_trend_review.py \
  -q -k 'strategy_snapshot or target_weight or boiling or replay'
```

Expected: PASS; current versions use the new map and old versions retain their
frozen map.

- [ ] **Step 6: Commit**

```bash
git add src/open_trader/a_share_trend.py src/open_trader/trend_review.py \
  tests/test_a_share_trend.py tests/test_market_trend.py \
  tests/test_trend_review.py
git commit -m "feat: publish trend discipline v9 and v6"
```

---

### Task 2: 切换新版本持仓退出，同时保留旧版部分卖出

**Files:**
- Modify: `src/open_trader/a_share_trend.py:2362-2430`
- Modify: `src/open_trader/a_share_trend.py:2640-2830`
- Test: `tests/test_a_share_trend.py:2380-3025`

**Interfaces:**
- Consumes: `CURRENT_EXIT_DISCIPLINES` from Task 1.
- Changes: `_holding_action(..., current_exit_discipline: bool = False)`.
- Preserves: default `False` is legacy behavior for direct old tests and old snapshots.
- Produces: new reports never emit `SELL_PARTIAL`; old snapshots still can.

- [ ] **Step 1: Write failing cross-market exit tests**

Add:

```python
@pytest.mark.parametrize(
    ("market", "version"),
    [("CN", "v9"), ("US", "v6"), ("HK", "v6")],
)
def test_current_exit_discipline_ignores_overheat_and_sells_on_flat(
    market: str, version: str,
) -> None:
    pools = (622466, 697199) if market == "CN" else (622460,) if market == "US" else (622494,)
    strategy = trend_module.live_trend_strategy_snapshot(
        market, "abc123", pools, strategy_version=version
    )
    common = {
        "as_of_date": "2026-07-14",
        "execution_date": "2026-07-15",
        "account": account("600001"),
        "candidates": (),
        "bars_by_symbol": {"600001": bars(close=12, low=11)},
        "market": market,
        "strategy_snapshot": strategy,
    }
    overheated = build_report(
        **common,
        holding_snapshots={
            "600001": holding("600001", boiling=True, champagne=True)
        },
    )
    flat = build_report(
        **common,
        holding_snapshots={
            "600001": holding(
                "600001", temperature_prev="热", temperature_curr="平"
            )
        },
    )

    assert (overheated.holdings[0].action, overheated.holdings[0].reason) == (
        "HOLD", "trend_intact"
    )
    assert (flat.holdings[0].action, flat.holdings[0].reason) == (
        "SELL_ALL", "temperature_changed_to_flat"
    )


def test_current_exit_discipline_preserves_existing_line_without_trailing() -> None:
    strategy = trend_module.live_trend_strategy_snapshot(
        "CN", "abc123", (622466, 697199), strategy_version="v9"
    )
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account("600001"),
        candidates=(),
        holding_snapshots={"600001": holding("600001", boiling=True)},
        bars_by_symbol={"600001": bars(close=12, low=11)},
        strategy_snapshot=strategy,
        prior_state={
            "schema_version": 1,
            "positions": {
                "600001": {
                    "initial_line": "8",
                    "active_line": "9",
                    "atr14": "1",
                    "tracking_active": True,
                    "position_started_for": "2026-07-01",
                    "updated_for": "2026-07-13",
                }
            },
        },
    )
    assert built.holdings[0].active_line == Decimal("9")
    assert built.protection_state["positions"]["600001"]["tracking_active"] is False


def test_current_exit_discipline_does_not_require_overheat_fields() -> None:
    strategy = trend_module.live_trend_strategy_snapshot(
        "US", "abc123", (622460,), strategy_version="v6"
    )
    built = build_report(
        as_of_date="2026-07-14",
        execution_date="2026-07-15",
        account=account("600001"),
        candidates=(),
        holding_snapshots={
            "600001": holding("600001", boiling=None, champagne=None)
        },
        bars_by_symbol={"600001": bars()},
        market="US",
        strategy_snapshot=strategy,
    )
    assert built.holdings[0].action == "HOLD"


@pytest.mark.parametrize(
    ("snapshot_changes", "triggered", "reason"),
    [
        ({"danger": True}, set(), "danger_signal"),
        ({"right_side": False}, set(), "left_trend_right_side"),
        ({}, {"600001"}, "protection_line_already_triggered"),
    ],
)
@pytest.mark.parametrize(
    ("market", "version"),
    [("CN", "v9"), ("US", "v6"), ("HK", "v6")],
)
def test_current_exit_discipline_keeps_all_existing_full_exit_triggers(
    market: str,
    version: str,
    snapshot_changes: dict[str, object],
    triggered: set[str],
    reason: str,
) -> None:
    snapshot = replace(holding("600001"), **snapshot_changes)
    assert trend_module._holding_action(
        symbol="600001",
        snapshot=snapshot,
        triggered=triggered,
        market=market,
        current_exit_discipline=True,
    ) == ("SELL_ALL", reason)
```

Retain the existing `test_explicit_overheat_creates_one_partial_action`; it is
the runnable legacy compatibility check because it uses the legacy v3 snapshot.

- [ ] **Step 2: Run the new tests and confirm RED**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_a_share_trend.py -q \
  -k 'current_exit_discipline or explicit_overheat_creates_one_partial_action'
```

Expected: new-version cases currently generate partial exits, US/HK do not sell
on flat, and the old tracking line rises.

- [ ] **Step 3: Make `_holding_action` version-aware with one boolean**

Use the existing priority order and avoid a new strategy class:

```python
def _holding_action(
    *,
    symbol: str,
    snapshot: HoldingSnapshot | None,
    triggered: set[str],
    market: str = "CN",
    overheat_trim_terminal: bool = False,
    current_exit_discipline: bool = False,
) -> tuple[str, str]:
    if symbol in triggered:
        return "SELL_ALL", "protection_line_already_triggered"
    if snapshot is not None and snapshot.danger is True:
        return "SELL_ALL", "danger_signal"
    if snapshot is not None and snapshot.right_side is False:
        return "SELL_ALL", "left_trend_right_side"
    temperature_exit = market == "CN" or current_exit_discipline
    if (
        temperature_exit
        and snapshot is not None
        and snapshot.temperature_prev in {"温", "热", "沸"}
        and snapshot.temperature_curr == "平"
    ):
        return "SELL_ALL", "temperature_changed_to_flat"
    if (
        not current_exit_discipline
        and snapshot is not None
        and (snapshot.boiling is True or snapshot.champagne is True)
        and not overheat_trim_terminal
    ):
        return "SELL_PARTIAL", "overheat_take_profit"
```

For the unknown-signal check, always require `right_side` and `danger`; require
temperature values when `temperature_exit` is true, and require
boiling/champagne only when `current_exit_discipline` is false.

- [ ] **Step 4: Stop new tracking without lowering old lines**

In `build_report()` calculate:

```python
current_exit_discipline = (
    market, snapshot_version
) in CURRENT_EXIT_DISCIPLINES
```

Pass it to `_holding_action()`. Only enable and update `tracking_active` inside
`if not current_exit_discipline`; for current versions force the persisted flag
to `False` after reading `initial_line` and `active_line`. Do not delete the old
`overheat_trim_*` state fields: copy them unchanged so old frozen actions can
still be reconciled.

- [ ] **Step 5: Run all holding and output compatibility tests**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_a_share_trend.py -q \
  -k 'holding or overheat or protection or partial_action'
```

Expected: PASS, including old partial-exit tests and new all-market flat exits.

- [ ] **Step 6: Commit**

```bash
git add src/open_trader/a_share_trend.py tests/test_a_share_trend.py
git commit -m "feat: apply current trend exit discipline"
```

---

### Task 3: 跨版本继承回撤高水位和暂停状态

**Files:**
- Modify: `src/open_trader/strategy_drawdown.py:323-455`
- Modify: `src/open_trader/drawdown_preflight.py:19-225`
- Test: `tests/test_strategy_drawdown.py`
- Test: `tests/test_drawdown_preflight.py`
- Test: `tests/test_strategy_drawdown_cli.py`

**Interfaces:**
- Changes: `automatic_bootstrap_strategy_drawdown(..., inherit_from: tuple[str, str] | None = None)`.
- Produces: `APPROVED_DRAWDOWN_PREDECESSORS` with exactly CN v9←v8, US v6←v5, HK v6←v5.
- Preserves: all existing callers omit `inherit_from` and retain current behavior.

- [ ] **Step 1: Write failing inheritance and fail-closed tests**

Add a state-level test:

```python
def test_new_version_inherits_high_water_mark_and_pause_state(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    old = {
        "market": "CN",
        "strategy_id": "trend_animals_warm_to_hot/CN/v8",
        "strategy_version": "v8",
    }
    bootstrap(data_dir, old, equity="100")
    observe_strategy_equity(
        data_dir,
        **old,
        current_equity=Decimal("94"),
        observed_at="2026-07-24T15:00:00+08:00",
    )

    decision = automatic_bootstrap_strategy_drawdown(
        data_dir,
        market="CN",
        strategy_id="trend_animals_warm_to_hot/CN/v9",
        strategy_version="v9",
        parameters={"drawdown_limit": "0.05"},
        baseline_equity=Decimal("96"),
        source_date="2026-07-24",
        accepted_git_sha="b" * 40,
        actor="pytest",
        occurred_at="2026-07-25T08:00:00+08:00",
        reason="new_strategy_version",
        entry_eligible_from="2026-07-27",
        inherit_from=("trend_animals_warm_to_hot/CN/v8", "v8"),
    )

    assert decision["high_water_mark"] == "100"
    assert decision["current_equity"] == "96"
    assert decision["entry_allowed"] is False
    assert decision["paused_at"] == "2026-07-24T15:00:00+08:00"
```

Add a preflight test that seeds v8/v5 records, requests v9/v6, and asserts all
three new records inherit their market's old high water. Add another test where
the approved predecessor is missing and assert `run_drawdown_preflight()` returns
`failed`, leaves the state bytes unchanged, and reports
`approved predecessor drawdown state is unavailable`.

- [ ] **Step 2: Run the drawdown tests and confirm RED**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_strategy_drawdown.py \
  tests/test_drawdown_preflight.py \
  -q -k 'inherits_high_water or approved_predecessor'
```

Expected: `automatic_bootstrap_strategy_drawdown()` rejects `inherit_from` and
preflight currently rebases from the new baseline.

- [ ] **Step 3: Copy one approved predecessor record atomically**

Add the optional keyword argument. After loading `records`, validate the source:

```python
predecessor_key = (
    _strategy_key(key[0], inherit_from[0], inherit_from[1])
    if inherit_from is not None
    else None
)
if predecessor_key is not None and reason != "new_strategy_version":
    raise ValueError("drawdown inheritance requires a new strategy version")
```

When the target record is absent, resolve the predecessor before creating the
target:

```python
predecessor = next(
    (
        item for item in records
        if predecessor_key is not None and _record_key(item) == predecessor_key
    ),
    None,
)
if predecessor_key is not None and not isinstance(predecessor, dict):
    raise ValueError("approved predecessor drawdown state is unavailable")

record = _new_record(key, equity=equity, updated_at=occurred_at)
if isinstance(predecessor, dict):
    inherited_high = _positive_decimal(
        predecessor["high_water_mark"], "inherited high_water_mark"
    )
    inherited_paused = predecessor["paused"] is True
    high_water = inherited_high if inherited_paused else max(inherited_high, equity)
    drawdown = _drawdown(high_water, equity)
    paused = inherited_paused or drawdown >= DRAWDOWN_LIMIT
    record.update({
        "high_water_mark": _decimal_text(high_water),
        "current_equity": _decimal_text(equity),
        "drawdown_pct": _decimal_text(drawdown),
        "paused": paused,
        "paused_at": (
            predecessor["paused_at"]
            if inherited_paused
            else occurred_at if paused else None
        ),
    })
```

Use the existing atomic `_write_state()` path; do not add a second state file or
event type.

- [ ] **Step 4: Restrict preflight to the three approved transitions**

Add:

```python
APPROVED_DRAWDOWN_PREDECESSORS = {
    ("CN", "v9"): ("trend_animals_warm_to_hot/CN/v8", "v8"),
    ("US", "v6"): ("trend_animals_warm_to_hot/US/v5", "v5"),
    ("HK", "v6"): ("trend_animals_warm_to_hot/HK/v5", "v5"),
}
```

Pass the exact mapping value only when `reason == "new_strategy_version"`.
Existing and first-activation versions continue to pass `None`. Never fall back
to a fresh baseline when an approved predecessor mapping exists but its record
is unavailable.

Update the drawdown CLI expectations so its default/current versions are
CN `v9` and US/HK `v6`, while retaining explicit historical-version coverage.

- [ ] **Step 5: Run the complete drawdown suites**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_strategy_drawdown.py \
  tests/test_drawdown_preflight.py \
  tests/test_strategy_drawdown_cli.py \
  -q
```

Expected: PASS; old bootstrap/compatibility tests remain unchanged.

- [ ] **Step 6: Commit**

```bash
git add src/open_trader/strategy_drawdown.py \
  src/open_trader/drawdown_preflight.py \
  tests/test_strategy_drawdown.py tests/test_drawdown_preflight.py \
  tests/test_strategy_drawdown_cli.py
git commit -m "feat: inherit trend drawdown across versions"
```

---

### Task 4: 传播版本契约并展示新纪律

**Files:**
- Modify: `src/open_trader/trend_review.py:80-82`
- Modify: `src/open_trader/trend_review.py:2770-2795`
- Modify: `src/open_trader/trend_review.py:4360-4390`
- Modify: `src/open_trader/trend_review.py:5065-5090`
- Modify: `src/open_trader/trend_review.py:5325-5630`
- Modify: `src/open_trader/dashboard.py:1390-1565`
- Modify: `src/open_trader/dashboard_acceptance.py:49-53`
- Modify: `src/open_trader/a_share_trend.py:3128-3665`
- Modify: `src/open_trader/dashboard_static/dashboard.js:1920-1935`
- Delete: `src/open_trader/dashboard_static/dashboard.js:2637-2664`
- Test: `tests/test_trend_review.py:6180-6230`
- Test: `tests/test_dashboard.py:1848-1960`
- Test: `tests/test_dashboard_web.py:9360-9425`
- Test: `tests/test_dashboard_acceptance.py:1120-1320`
- Test: `tests/test_a_share_trend.py:3340-3470`

**Interfaces:**
- Consumes: new version snapshots and report fields from Tasks 1–2.
- Preserves: `SELL_PARTIAL` labels and renderer branches for old frozen reports.
- Produces: current report reason copy that distinguishes initial ATR protection
  from an inherited raised line without changing old report text.

- [ ] **Step 1: Write failing version-propagation and display tests**

Update current-version parametrizations to:

```python
[
    ("CN", "v9"),
    ("US", "v6"),
    ("HK", "v6"),
]
```

Update approved mixed identities to:

```python
[
    ("CN", ("v4", "v7", "v8", "v9")),
    ("US", ("v4", "v5", "v6")),
    ("HK", ("v4", "v5", "v6")),
]
```

Add a Dashboard JavaScript test with one current snapshot and one historical
snapshot:

```javascript
const current = renderTrendReportWorkspace({
  ...report("CN"),
  strategy_version: "v9",
  strategy_parameter_rows: [
    {group:"仓位执行",name:"目标仓位",value:"账户净值的 4%"},
    {group:"退出保护",name:"退出条件",value:"危险、离开右侧、转平或保护线清仓"},
  ],
});
if (!current.includes("目标仓位") || current.includes("止盈减仓 30%")
    || current.includes("沸状态仓位")) throw new Error(current);

const historical = renderTrendReportWorkspace({
  ...report("US"),
  strategy_version: "v5",
  strategy_parameter_rows: [
    {group:"退出保护",name:"过热止盈比例",value:"沸腾或开香槟时减仓 30%"},
  ],
  sell_actions: [{
    action:"SELL_PARTIAL",symbol:"AAPL",name:"Apple",
    reason:"overheat_take_profit",estimated_shares:3,
  }],
});
if (!historical.includes("止盈减仓 30%")
    || !historical.includes("沸腾或开香槟时减仓 30%")) throw new Error(historical);
```

Add current Markdown/Feishu assertions that no zero-count
`止盈减仓 30%` text is emitted. Keep existing old partial output tests.

- [ ] **Step 2: Run focused projection and UI tests and confirm RED**

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_trend_review.py \
  tests/test_dashboard.py \
  tests/test_dashboard_web.py \
  tests/test_dashboard_acceptance.py \
  tests/test_a_share_trend.py \
  -q -k 'current_live_strategy_versions or approved_mixed or current_exit_copy or historical_partial'
```

Expected: current v9/v6 reports are rejected by one or more allowlists and
current Markdown still prints the partial-profit count.

- [ ] **Step 3: Extend existing allowlists, not the schemas**

Add `v9` anywhere the existing risk/replay logic lists `v2` through `v8`.
Allow market/version pairs exactly as follows:

```python
TREND_ACCEPTED_STRATEGY_VERSIONS = {
    "CN": frozenset({"v4", "v6", "v7", "v8", "v9"}),
    "US": frozenset({"v4", "v5", "v6"}),
    "HK": frozenset({"v4", "v5", "v6"}),
}
```

In `trend_review.py`, include `v9` in `TREND_STRATEGY_VERSIONS`, replay input
requirements, risk reconstruction and expected live snapshot normalization.
Do not remove old versions.

- [ ] **Step 4: Make new output omit partial-profit copy**

In `render_markdown()` and Feishu summary construction, calculate:

```python
strategy_version = str(report.strategy_snapshot.get("strategy_version") or "")
current_exit_discipline = (
    market, strategy_version
) in CURRENT_EXIT_DISCIPLINES
```

Only append the `止盈减仓 30%` count when
`current_exit_discipline` is false. Keep the existing `SELL_PARTIAL` action
formatting unchanged for historical reports.

For protection reasons, use a small output-only helper:

```python
def _holding_reason_label(
    item: Mapping[str, object],
    *,
    current_exit_discipline: bool,
) -> str:
    reason = str(item.get("reason") or "")
    if reason != "protection_line_already_triggered" or not current_exit_discipline:
        return _reason_label(reason)
    initial = _optional_decimal(item.get("initial_line"))
    active = _optional_decimal(item.get("active_line"))
    return (
        "2×ATR14 硬止损"
        if initial is not None and active == initial
        else "既有活动保护线触发"
    )
```

Use it in new-version Markdown and Feishu. In Dashboard JavaScript, pass the
report version into the existing action renderer and apply the same
`initial_line === active_line` distinction; old versions continue to use
`TREND_REASON_LABELS.protection_line_already_triggered`.

Delete the uncalled `renderCnTrendDisciplines()` hard-coded duplicate. Keep
`TREND_DISCIPLINE_ROW_NAMES`, `SELL_PARTIAL` labels and historical render paths.

- [ ] **Step 5: Run all affected projection and Dashboard tests**

Use the repository root so ignored historical snapshots are available:

```bash
WORKTREE=$(pwd -P)
REPOSITORY_ROOT=$(git rev-parse --path-format=absolute --git-common-dir)/..
cd "$REPOSITORY_ROOT"
PYTHONSAFEPATH=1 \
PYTHONPATH="$WORKTREE:$WORKTREE/src" \
"$WORKTREE/.venv/bin/python" -m pytest \
  "$WORKTREE/tests/test_trend_review.py" \
  "$WORKTREE/tests/test_dashboard.py" \
  "$WORKTREE/tests/test_dashboard_web.py" \
  "$WORKTREE/tests/test_dashboard_acceptance.py" \
  "$WORKTREE/tests/test_a_share_trend.py" \
  -q
```

Expected: PASS with no missing historical fixture errors.

- [ ] **Step 6: Commit**

```bash
git add src/open_trader/a_share_trend.py \
  src/open_trader/trend_review.py \
  src/open_trader/dashboard.py \
  src/open_trader/dashboard_acceptance.py \
  src/open_trader/dashboard_static/dashboard.js \
  tests/test_a_share_trend.py \
  tests/test_trend_review.py \
  tests/test_dashboard.py \
  tests/test_dashboard_web.py \
  tests/test_dashboard_acceptance.py
git commit -m "feat: present current trend exit discipline"
```

---

### Task 5: 同步纪律文档和合并日志

**Files:**
- Modify: `纪律.md`
- Modify: `CONTEXT.md`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Test: repository text scans

**Interfaces:**
- Consumes: exact version identities and behavior from Tasks 1–4.
- Produces: operator-facing release entry required before merge.

- [ ] **Step 1: Write the expected-text scan and confirm it fails**

Run:

```bash
rg -n '机器策略版本：CN v9|热和沸.*4%|CN v4、CN v7、CN v8、CN v9' 纪律.md
rg -n '旧版本.*过热止盈减仓|新版本.*不再.*30%' CONTEXT.md README.md
rg -n 'CN v9|US v6|HK v6' CHANGELOG.md
```

Expected: at least the new version and new inheritance scans return no match.

- [ ] **Step 2: Update the human discipline without erasing history**

In `纪律.md`:

- set the machine versions to `CN v9 / US v6 / HK v6`;
- replace temperature-specific A-share sizing with one 4% target;
- keep the initial protection formula;
- list the four full-exit triggers;
- remove current overheat partial-profit and trailing-line instructions;
- state that legacy raised lines stay fixed and cannot be lowered;
- record the exact Kelly inheritance lists.

In `CONTEXT.md`, change “Kelly 目标仓位” to the 4% ceiling for both hot and
boiling. Mark “过热止盈减仓” and “跟踪保护线” as old-version terms retained
only for frozen reports and unfinished actions. Define the current activity line
as initial `2 × ATR14` or a higher line inherited from an old version.

In `README.md`, keep the `SELL_PARTIAL` resolution instructions but prefix them
as legacy frozen-action handling. State that v9/v6 never create new partial
actions.

- [ ] **Step 3: Add the dated operator changelog entry**

Under `## 2026-07-25`, add:

```markdown
- Published CN v9, US v6, and HK v6 trend discipline: A-share hot/boiling
  entries now share the 4% ceiling; new reports no longer create 30% overheat
  trims or trailing-line raises; danger, right-side exit, temperature-flat, and
  2×ATR14 protection still sell all. Existing Kelly samples, drawdown state,
  raised protection lines, and frozen partial exits remain compatible.
```

- [ ] **Step 4: Run text and diff checks**

```bash
rg -n '沸状态仓位.*2%|生成一次“止盈减仓 30%”' 纪律.md
rg -n 'CN v9|US v6|HK v6|2 × ATR14' 纪律.md CONTEXT.md README.md CHANGELOG.md
git diff --check
```

Expected: the first command returns no current-discipline matches in
`纪律.md`; the second finds all new identities and the ATR rule; `git diff
--check` exits 0.

- [ ] **Step 5: Commit**

```bash
git add 纪律.md CONTEXT.md README.md CHANGELOG.md
git commit -m "docs: record trend discipline v9 and v6"
```

---

### Task 6: 真实流程、最终 acceptance 和同 SHA 复部署

**Files:**
- Verify only: committed source, live `data/`, `reports/`, launchd status and logs
- No source edits after the final acceptance command starts

**Interfaces:**
- Consumes: clean committed branch from Tasks 1–5.
- Produces: one accepted SHA, fresh controller/Dashboard PIDs and review URL.

- [ ] **Step 1: Run the complete automated suite from the data-bearing root**

```bash
WORKTREE=$(pwd -P)
REPOSITORY_ROOT=$(git rev-parse --path-format=absolute --git-common-dir)/..
cd "$REPOSITORY_ROOT"
PYTHONSAFEPATH=1 \
PYTHONPATH="$WORKTREE:$WORKTREE/src" \
"$WORKTREE/.venv/bin/python" -m pytest "$WORKTREE/tests" -q
```

Expected: all tests pass; record the exact count and duration.

- [ ] **Step 2: Inspect old processes before changing live state**

```bash
launchctl list | rg 'com\.open-trader\.(trend|premarket)'
pgrep -f 'open_trader trend-market run' | xargs ps -o pid,lstart,command -p || true
screen -ls | rg 'open_trader_dashboard_8766' || true
```

Record controller and Dashboard PIDs, start times, working directories and SHAs.

- [ ] **Step 3: Run the affected preflight directly**

From the worktree:

```bash
PYTHONPATH=src .venv/bin/python -m open_trader trend-drawdown-preflight \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env \
  --repo /Users/ray/projects/open_trader/.worktrees/trend-exit-discipline-v9 \
  --actor acceptance
```

Expected: CN/HK/US are `ready` or `bootstrapped`, the new records retain old
high-water marks, and no market reports `failed` or `unavailable`.

- [ ] **Step 4: Deploy the committed candidate for acceptance**

```bash
cd /Users/ray/projects/open_trader/.worktrees/trend-exit-discipline-v9
ACCEPTED_SHA=$(git rev-parse HEAD)
test -z "$(git status --short)"
scripts/install_daily_premarket_launchd.sh \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env \
  --trend-only --market all

screen -S open_trader_dashboard_8766 -X quit 2>/dev/null || true
screen -dmS open_trader_dashboard_8766 zsh -lc \
  'cd /Users/ray/projects/open_trader/.worktrees/trend-exit-discipline-v9 && exec env PYTHONPATH=src .venv/bin/python -u -m open_trader dashboard --portfolio /Users/ray/projects/open_trader/data/latest/portfolio.csv --data-dir /Users/ray/projects/open_trader/data --reports-dir /Users/ray/projects/open_trader/reports --config /Users/ray/projects/open_trader/config/daily_premarket.env --poll-seconds 5 --host 127.0.0.1 --port 8766 >> /tmp/open_trader_dashboard_8766.log 2>&1'
```

- [ ] **Step 5: Verify the direct live workflow before the final gate**

For each market:

```bash
PYTHONPATH=src .venv/bin/python -m open_trader trend-market status \
  --market CN \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env
PYTHONPATH=src .venv/bin/python -m open_trader trend-market status \
  --market HK \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env
PYTHONPATH=src .venv/bin/python -m open_trader trend-market status \
  --market US \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env
```

Then inspect fresh logs:

```bash
pgrep -f 'open_trader trend-market run' | xargs ps -o pid,lstart,command -p
tail -n 100 logs/daily_premarket/launchd-trend-controller-*.out.log
tail -n 100 logs/daily_premarket/launchd-trend-controller-*.err.log
tail -n 100 /tmp/open_trader_dashboard_8766.log
```

Expected: all status documents show the candidate worktree and SHA, new PIDs
are live, heartbeats advance, and fresh logs contain no traceback or stale
version.

- [ ] **Step 6: Run `make acceptance` as the final gate**

```bash
make acceptance
```

Expected terminal result: `PASS`. If it is `FAIL`, diagnose, modify, rerun
focused/full tests, commit the fix, redeploy the new candidate and repeat this
step. If it is `BLOCKED`, report the blocker and do not substitute another
check.

- [ ] **Step 7: Redeploy the exact accepted SHA**

Do not edit source or data between acceptance and this restart:

```bash
cd /Users/ray/projects/open_trader/.worktrees/trend-exit-discipline-v9
test "$(git rev-parse HEAD)" = "$ACCEPTED_SHA"
test -z "$(git status --short)"

scripts/install_daily_premarket_launchd.sh \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env \
  --trend-only --market all

screen -S open_trader_dashboard_8766 -X quit 2>/dev/null || true
screen -dmS open_trader_dashboard_8766 zsh -lc \
  'cd /Users/ray/projects/open_trader/.worktrees/trend-exit-discipline-v9 && exec env PYTHONPATH=src .venv/bin/python -u -m open_trader dashboard --portfolio /Users/ray/projects/open_trader/data/latest/portfolio.csv --data-dir /Users/ray/projects/open_trader/data --reports-dir /Users/ray/projects/open_trader/reports --config /Users/ray/projects/open_trader/config/daily_premarket.env --poll-seconds 5 --host 127.0.0.1 --port 8766 >> /tmp/open_trader_dashboard_8766.log 2>&1'
```

- [ ] **Step 8: Prove the exact-SHA deployment**

```bash
export ACCEPTED_SHA
.venv/bin/python - <<'PY'
from datetime import datetime
import json
import os
from pathlib import Path
import time

accepted_sha = os.environ["ACCEPTED_SHA"]
worktree = "/Users/ray/projects/open_trader/.worktrees/trend-exit-discipline-v9"
root = Path("/Users/ray/projects/open_trader/data/trend_controller")

def read(market: str) -> dict[str, object]:
    return json.loads(
        (root / market / "status.json").read_text(encoding="utf-8")
    )

before = {market: read(market) for market in ("CN", "HK", "US")}
time.sleep(10)
for market, previous in before.items():
    current = read(market)
    pid = int(current["pid"])
    os.kill(pid, 0)
    assert current["working_directory"] == worktree
    assert current["git_sha"] == accepted_sha
    assert datetime.fromisoformat(str(current["heartbeat_at"])) > datetime.fromisoformat(
        str(previous["heartbeat_at"])
    )
    print(market, pid, current["git_sha"], current["heartbeat_at"])
PY

pgrep -f 'open_trader trend-market run' | xargs ps -o pid,lstart,command -p
tail -n 80 logs/daily_premarket/launchd-trend-controller-*.out.log
tail -n 80 logs/daily_premarket/launchd-trend-controller-*.err.log
tail -n 80 /tmp/open_trader_dashboard_8766.log
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/
curl -sS http://127.0.0.1:8766/api/dashboard | .venv/bin/python -m json.tool >/dev/null
```

Expected: three advancing heartbeats from new PIDs, exact worktree and accepted
SHA, fresh clean logs, HTTP `200`, and valid Dashboard JSON. Provide
`http://127.0.0.1:8766/` to the user only after all assertions pass.
