# 趋势复盘完整交易胜率 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在“策略与市场基准”的现有两列统计摘要中，分别展示纪律模拟与实际执行的成本后完整交易胜率。

**Architecture:** `trend_api_stats` 继续作为完整持仓闭环和胜负判定的唯一所有者；`trend_statistics_disposition` 只把已有合格闭环聚合成胜出数和胜率。趋势复盘投影升级为严格 v5 契约，Dashboard 只格式化并展示这两个来源各自的统计，不重算闭环，也不给市场基准制造胜率。

**Tech Stack:** Python 3.12、Decimal、pytest、原生 JavaScript、Playwright Dashboard acceptance、launchd。

## Global Constraints

- 纪律模拟与实际执行分开统计，不合并样本。
- 胜率只使用现有 `_partition_round_dispositions` 选出的合格完整闭环。
- `net_pnl > 0` 才算胜；`net_pnl == 0` 进入分母但不计胜。
- 少于 30 个闭环仍展示胜率；30 笔门槛继续只影响 Kelly 和现有样本提示。
- 统计来源不可用或合格闭环为 0 时不得显示虚假的 `0%`。
- 投影版本必须从 `open_trader.trend_review.projection.v4` 升为 `open_trader.trend_review.projection.v5`。
- 不改交易策略、执行、Kelly、市场基准、数据库、依赖或报告布局。
- 不新增 Dashboard 指标轴、卡片、市场“不适用”项或 CSS。
- 所有实现与修复由 `worker` 完成；实现验证后由 `reviewer` 审查，修复后重复审查直到无可执行问题。
- `make acceptance` 只能在所有源码、测试、CHANGELOG 和 reviewer 修复完成后运行一次最终 gate；只有 `PASS` 才能交付。

## File Map

- `src/open_trader/trend_api_stats.py`：从已有合格闭环产出 `winning_sample_count` 与 `win_rate`。
- `tests/test_trend_api_stats.py`：固定赢、亏、持平、来源隔离和无来源语义。
- `src/open_trader/trend_review.py`：把两条 disposition 写入 v5 投影。
- `src/open_trader/dashboard.py`：严格验证 v5 样本字段、数量与比率一致性；继续覆盖最新统计快照。
- `src/open_trader/trend_market_controller.py`：把 v4 视为需要重建，只认 v5 为当前投影。
- `tests/test_trend_review.py`、`tests/test_dashboard.py`、`tests/test_trend_market_controller.py`：覆盖 v5 生成、拒绝损坏投影和旧版重建。
- `src/open_trader/dashboard_static/dashboard.js`：在已有两列统计摘要中渲染方案 A。
- `src/open_trader/dashboard_acceptance.py`：从真实 API/DOM 验证两条胜率文案，同时保持五指标、五系列不变。
- `tests/test_dashboard_web.py`、`tests/test_dashboard_acceptance.py`：固定桌面/移动共享渲染、零样本和来源不可用文案。
- `CHANGELOG.md`：记录用户可见行为、验证范围和真实工作流结果。

---

### Task 1: 从规范闭环公开每个来源的胜出数和胜率

**Files:**
- Modify: `src/open_trader/trend_api_stats.py:1812-1860`
- Test: `tests/test_trend_api_stats.py:1018-1078`

**Interfaces:**
- Consumes: `_partition_round_dispositions(rounds, target) -> (eligible, excluded)`；每个 eligible round 已含 `result`。
- Produces: `trend_statistics_disposition(payload, *, market, strategy_id, opening_strategy_version, source)` 新增 `winning_sample_count: int` 与 `win_rate: str | None`。

- [ ] **Step 1: 写出按来源分离且把持平计入分母的失败测试**

在 `tests/test_trend_api_stats.py` 增加：

```python
def test_statistics_disposition_reports_win_rate_per_source_and_counts_flat() -> None:
    def pair(
        label: str, *, source: str, sell_price: str, day: int,
    ) -> list[dict[str, object]]:
        broker, account_id = (
            ("futu", "101") if source == "simulation" else ("tiger", "U1")
        )
        rows = [
            fill(
                f"{label}-buy", side="buy", quantity="1", price="10", fee="0",
                filled_at=f"2026-08-{day:02d}T10:00:00-04:00",
                source=source, broker=broker, account_id=account_id,
            ),
            fill(
                f"{label}-sell", side="sell", quantity="1", price=sell_price,
                fee="0", filled_at=f"2026-08-{day + 1:02d}T10:00:00-04:00",
                source=source, broker=broker, account_id=account_id,
            ),
        ]
        for row in rows:
            row["symbol"] = label
        return rows

    payload = build_trend_api_stats_payload(
        [
            *pair("SIM-WIN", source="simulation", sell_price="12", day=1),
            *pair("SIM-FLAT", source="simulation", sell_price="10.01", day=3),
            *pair("ACTUAL-LOSS", source="actual", sell_price="9", day=5),
        ],
        strategy_versions=[],
        generated_at="2026-08-07T17:00:00-04:00",
        statistics_cutoff_at="2026-08-07T16:00:00-04:00",
    )
    payload["sources"] = [
        {
            "source": "simulation", "source_id": "simulation:futu:101",
            "broker": "futu", "account_id": "101", "market": "US",
            "orders_seen": 4, "fill_count": 4,
            "statistics_cutoff_at": "2026-08-07T16:00:00-04:00",
            "status": "available",
        },
        {
            "source": "actual", "source_id": "actual:tiger:U1",
            "broker": "tiger", "account_id": "U1", "market": "US",
            "orders_seen": 2, "fill_count": 2,
            "statistics_cutoff_at": "2026-08-07T16:00:00-04:00",
            "status": "available",
        },
    ]

    simulation = trend_statistics_disposition(
        payload, market="US", strategy_id="trend_animals_warm_to_hot/US/v1",
        opening_strategy_version="v1", source="simulation",
    )
    actual = trend_statistics_disposition(
        payload, market="US", strategy_id="trend_animals_warm_to_hot/US/v1",
        opening_strategy_version="v1", source="actual",
    )
    unavailable = trend_statistics_disposition(
        payload, market="HK", strategy_id="trend_animals_warm_to_hot/HK/v1",
        opening_strategy_version="v1", source="actual",
    )

    assert simulation["eligible_sample_count"] == 2
    assert simulation["winning_sample_count"] == 1
    assert simulation["win_rate"] == "0.5"
    assert actual["eligible_sample_count"] == 1
    assert actual["winning_sample_count"] == 0
    assert actual["win_rate"] == "0"
    assert unavailable["winning_sample_count"] == 0
    assert unavailable["win_rate"] is None
```

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_trend_api_stats.py::test_statistics_disposition_reports_win_rate_per_source_and_counts_flat
```

Expected: FAIL with `KeyError: 'winning_sample_count'`.

- [ ] **Step 3: 在唯一共享聚合点增加最小实现**

在 `trend_statistics_disposition` 中，对无来源返回值加入：

```python
"winning_sample_count": 0,
"win_rate": None,
```

在得到 `eligible` 后只计算一次：

```python
wins = sum(round_["result"] == "win" for round_ in eligible)
```

在可用返回值加入：

```python
"winning_sample_count": wins,
"win_rate": (
    _decimal_text(_divide(Decimal(wins), Decimal(len(eligible))))
    if eligible else None
),
```

不要改变 `_build_round`、`_strategy_stats`、Kelly adapter 或持仓闭环分组。

- [ ] **Step 4: 运行 Task 1 聚焦测试并确认 GREEN**

Run:

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_trend_api_stats.py::test_statistics_disposition_reports_win_rate_per_source_and_counts_flat \
  tests/test_trend_api_stats.py::test_statistics_disposition_conserves_every_candidate \
  tests/test_trend_api_stats.py::test_simulation_disposition_count_equals_calculate_trend_kelly_eligible_count
```

Expected: all selected tests PASS.

- [ ] **Step 5: 提交 Task 1**

```bash
git add src/open_trader/trend_api_stats.py tests/test_trend_api_stats.py
git commit -m "feat: expose trend review outcome counts"
```

---

### Task 2: 升级并严格验证趋势复盘 v5 投影

**Files:**
- Modify: `src/open_trader/trend_review.py:8302-8326,8562-8573`
- Modify: `src/open_trader/dashboard.py:516-560,653-780`
- Modify: `src/open_trader/trend_market_controller.py:188-198`
- Test: `tests/test_trend_review.py:7750-7810,10899-10980`
- Test: `tests/test_dashboard.py:1210-1235,1349-1410,1530-1540,1675-1693`
- Test: `tests/test_trend_market_controller.py:4667-4697,4730-4735`

**Interfaces:**
- Consumes: Task 1 disposition fields `eligible_sample_count`, `winning_sample_count`, `win_rate`。
- Produces: `open_trader.trend_review.projection.v5`；Dashboard 只接受内部一致的 v5；Controller 把 v4 视为旧投影。

- [ ] **Step 1: 把测试 fixture 升到 v5 并写出严格拒绝条件**

在所有规范 sample detail fixture 中加入：

```python
"winning_sample_count": eligible_sample_count,
"win_rate": "1" if eligible_sample_count else None,
```

实际执行 fixture 可使用 `winning_sample_count = 0` 和 `win_rate = "0"` 表示已有闭环但无胜出。更新 schema 断言为：

```python
assert projection["schema_version"] == "open_trader.trend_review.projection.v5"
```

在 Dashboard 无效投影参数化用例中加入四个 mutation：

```python
lambda payload: payload.update(
    schema_version="open_trader.trend_review.projection.v4"
),
lambda payload: payload["sample_details"]["discipline"].update(
    winning_sample_count=32
),
lambda payload: payload["sample_details"]["discipline"].update(
    win_rate="0.5"
),
lambda payload: payload["sample_details"]["discipline"].pop("win_rate"),
```

把 Controller 旧投影恢复测试的输入改成 v4，重建结果和稳定状态改成 v5：

```python
{"schema_version": "open_trader.trend_review.projection.v4"}
{"schema_version": "open_trader.trend_review.projection.v5"}
```

- [ ] **Step 2: 运行 v5/验证/Controller 用例并确认 RED**

Run:

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_trend_review.py -k 'projection and (sample or schema)' \
  tests/test_dashboard.py -k 'trend_review_projection or latest_statistics' \
  tests/test_trend_market_controller.py -k 'trend_review_projection'
```

Expected: FAIL because production still emits and accepts v4 and sample detail validation does not know the two new fields.

- [ ] **Step 3: 把生成器和 Controller 的当前版本改为 v5**

只替换两个生产版本常量比较：

```python
"schema_version": "open_trader.trend_review.projection.v5"
```

```python
return projection.get("schema_version") == "open_trader.trend_review.projection.v5"
```

不要增加 v4/v5 双读分支；旧投影必须走现有重建机制。

- [ ] **Step 4: 严格校验新增 sample detail 字段**

在 `dashboard.py` 的 Decimal import 中加入 `Context` 和 `localcontext`。把严格字段集合加入：

```python
"winning_sample_count",
"win_rate",
```

在 `_valid_trend_review_sample_detail` 中按 28 位精度复算规范比率：

```python
eligible = value["eligible_sample_count"]
wins = value["winning_sample_count"]
raw_rate = value["win_rate"]
if type(wins) is not int or wins < 0 or wins > eligible:
    return False
if raw_rate is not None and (
    not isinstance(raw_rate, str) or not raw_rate.strip()
):
    return False
try:
    rate = None if raw_rate is None else Decimal(raw_rate)
    if rate is not None and not rate.is_finite():
        return False
    with localcontext(Context(prec=28)):
        expected_rate = Decimal(wins) / Decimal(eligible) if eligible else None
except (InvalidOperation, TypeError, ValueError, ZeroDivisionError):
    return False
if rate != expected_rate:
    return False
```

保留现有候选守恒、aware timestamp 和 unavailable reason 校验；无来源 detail 仍要求 `winning_sample_count == 0`、`win_rate is None`。

- [ ] **Step 5: 运行 Task 2 聚焦测试并确认 GREEN**

Run:

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_trend_api_stats.py \
  tests/test_trend_review.py \
  tests/test_dashboard.py \
  tests/test_trend_market_controller.py
```

Expected: all four files PASS;不接受仅运行 `-k` 子集作为 Task 2 完成证据。

- [ ] **Step 6: 提交 Task 2**

```bash
git add \
  src/open_trader/trend_review.py \
  src/open_trader/dashboard.py \
  src/open_trader/trend_market_controller.py \
  tests/test_trend_review.py \
  tests/test_dashboard.py \
  tests/test_trend_market_controller.py
git commit -m "feat: validate trend review win rate projection"
```

---

### Task 3: 在方案 A 的两列摘要中渲染胜率

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js:4038-4053`
- Modify: `src/open_trader/dashboard_acceptance.py:4072-4081,4623-4665`
- Test: `tests/test_dashboard_web.py:7477-7633`
- Test: `tests/test_dashboard_acceptance.py:1620-1704,4097-4138`

**Interfaces:**
- Consumes: v5 `sample_details.discipline|actual` 中的 `eligible_sample_count`、`winning_sample_count`、`win_rate`。
- Produces: 每个可用来源一条 `完整交易胜率 …` 文案；现有五指标、五系列 DOM 数量保持不变。

- [ ] **Step 1: 更新 JS fixture 并先写失败断言**

把测试 detail helper 改成接收胜出数和胜率：

```javascript
const detail=(eligible,wins,winRate,discovered,excluded,open,cutoff,reasons=[])=>({
  available:true,eligible_sample_count:eligible,winning_sample_count:wins,
  win_rate:winRate,discovered_candidate_count:discovered,
  excluded_candidate_count:excluded,incomplete_open_candidate_count:open,
  exclusion_reasons:reasons,statistics_cutoff_at:cutoff,reason:"",
});
```

为已闭环来源断言：

```javascript
"完整交易胜率 25% · 1 胜 / 4 闭环"
```

为来源可用但零闭环新增 fixture 和断言：

```javascript
"完整交易胜率 数据不足 · 0 闭环"
```

来源不可用的 detail 必须补齐：

```javascript
winning_sample_count:0,win_rate:null
```

从 `test_dashboard_trend_review_is_compact_exact_and_account_scoped` 的 forbidden 文案中移除单独的 `胜率`，同时保留并断言：

```javascript
if ((html.match(/class="trend-review-metric"/g)||[]).length!==5) throw new Error(html);
if ((html.match(/class="trend-review-series/g)||[]).length!==25) throw new Error(html);
```

- [ ] **Step 2: 运行 Dashboard web 测试并确认 RED**

Run:

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_dashboard_web.py::test_dashboard_trend_review_is_compact_exact_and_account_scoped \
  tests/test_dashboard_web.py::test_dashboard_trend_review_renders_independent_statistics_status
```

Expected: FAIL because `renderTrendReviewStatisticsMeta` does not emit the approved copy.

- [ ] **Step 3: 用现有百分比 helper 添加方案 A 的唯一 UI 分支**

在 `renderTrendReviewStatisticsMeta` 中定义：

```javascript
const winRate = detail?.available !== true
  ? ""
  : detail.eligible_sample_count > 0 && hasValue(detail.win_rate)
    ? `<span>完整交易胜率 ${escapeHtml(trendRiskPercent(detail.win_rate))} · ${escapeHtml(formatDisplayNumber(detail.winning_sample_count))} 胜 / ${escapeHtml(formatDisplayNumber(detail.eligible_sample_count))} 闭环</span>`
    : "<span>完整交易胜率 数据不足 · 0 闭环</span>";
```

把返回顺序固定为：

```javascript
${disposition}${winRate}${exclusions}
```

不要修改 `TREND_REVIEW_METRICS`、`TREND_REVIEW_SERIES` 或 `dashboard.css`。

- [ ] **Step 4: 扩展真实浏览器 acceptance 文案校验**

在 `_check_trend_review` 遍历每个可用 detail 时，把下列期望文案加入 `statistics_items`：

```python
eligible = detail["eligible_sample_count"]
if eligible:
    displayed_rate = _trend_review_display(
        {"value": Decimal(str(detail["win_rate"])) * Decimal("100")},
        percent=True,
    )
    statistics_items.append(
        f"完整交易胜率 {displayed_rate} · "
        f"{detail['winning_sample_count']} 胜 / {eligible} 闭环"
    )
else:
    statistics_items.append("完整交易胜率 数据不足 · 0 闭环")
```

更新 `tests/test_dashboard_acceptance.py` 的三市场 review fixture：可用样本补齐胜出数/胜率，不可用来源补齐 `0/null`。保持 `TREND_REVIEW_METRIC_SPECS` 为五项，fake locator 仍返回五组系列。

- [ ] **Step 5: 运行 Task 3 全部 Dashboard 聚焦测试并确认 GREEN**

Run:

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_dashboard_web.py \
  tests/test_dashboard_acceptance.py
```

Expected: both files PASS;断言继续证明只有 5 个 metric section、5 个 axis、25 个 series item。

- [ ] **Step 6: 提交 Task 3**

```bash
git add \
  src/open_trader/dashboard_static/dashboard.js \
  src/open_trader/dashboard_acceptance.py \
  tests/test_dashboard_web.py \
  tests/test_dashboard_acceptance.py
git commit -m "feat: show trend review win rates"
```

---

### Task 4: 真实数据证明、变更日志、reviewer 与最终发布 gate

**Files:**
- Modify: `CHANGELOG.md`
- Verify only: `data/latest/trend_api_stats.json`
- Verify only: `data/latest/trend_review_{cn,hk,us}.json`

**Interfaces:**
- Consumes: Tasks 1-3 的完整分支状态。
- Produces: 当前真实数据证明、operator-facing log、reviewer PASS、最终 `make acceptance` PASS 和 exact-SHA review deployment。

- [ ] **Step 1: 运行所有受影响测试文件**

Run:

```bash
/Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_trend_api_stats.py \
  tests/test_trend_review.py \
  tests/test_dashboard.py \
  tests/test_trend_market_controller.py \
  tests/test_dashboard_web.py \
  tests/test_dashboard_acceptance.py
```

Expected: all selected files PASS with exact pytest summary recorded in the handoff.

- [ ] **Step 2: 对当前真实统计做只读三市场投影检查**

Run from the feature worktree:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -c 'from pathlib import Path; from open_trader.trend_review import build_trend_review_projection; root=Path("/Users/ray/projects/open_trader/data"); [(lambda p: print(market, p["schema_version"], p["sample_details"]["discipline"]["winning_sample_count"], p["sample_details"]["discipline"]["eligible_sample_count"], p["sample_details"]["discipline"]["win_rate"], p["sample_details"]["actual"]["winning_sample_count"], p["sample_details"]["actual"]["eligible_sample_count"], p["sample_details"]["actual"]["win_rate"]))(build_trend_review_projection(root, market)) for market in ("CN", "HK", "US")]'
```

Expected:

- 三行 schema 都是 `open_trader.trend_review.projection.v5`。
- 若当前 HK 统计 cutoff 尚未前移，纪律模拟为 `1 / 6 / 0.1666666666666666666666666667`，实际执行为 `0 / 0 / None`。
- 若 cutoff 已前移，以同一 `trend_api_stats.json` 中合格 rounds 重新核对胜出数、分母和比率；不得拿旧 `1/6` 快照要求 DOM 相等。
- 该命令只读，不写 `/Users/ray/projects/open_trader/data`。

- [ ] **Step 3: 更新当日 CHANGELOG 并提交**

在 `## 2026-08-14` 下增加一条 operator-facing 记录，必须包含：

- 纪律模拟/实际执行分别统计；
- 完整成本后 `net_pnl > 0` 的口径；
- v5 投影升级和 v4 自动重建；
- 零闭环显示数据不足；
- 聚焦测试数量与真实三市场检查结果。

Then:

```bash
git add CHANGELOG.md
git commit -m "docs: log trend review win rates"
```

- [ ] **Step 4: 主 agent 发起 reviewer gate，并把修复送回同一 worker**

Reviewer 必须比较本分支相对 `main` 的全部变更与已批准 spec，检查：

- 统计口径、来源隔离、持平分母；
- v5 严格契约和 v4 重建；
- 方案 A 文案、零样本/无来源语义；
- 五指标/五系列未变化；
- 测试和 CHANGELOG 完整。

任何可执行 finding 都发送回原 `worker` 修复并提交；随后重跑受影响测试和 Step 2，只要发生代码变化就重新运行 `reviewer`，直到无可执行 finding。

- [ ] **Step 5: 仅在 reviewer 清零后运行最终 Dashboard acceptance**

Run once from the clean feature worktree:

```bash
make acceptance
```

Expected: terminal final status `PASS`。`FAIL` 必须回到 worker 修复并重走 reviewer；`BLOCKED` 必须报告 blocker，不能用 curl、fixture、Mock 或单测替代。

- [ ] **Step 6: 从完全相同的已接受 SHA 重启 Controller 与 Dashboard**

先记录：

```bash
accepted_trend_win_rate_sha="$(git rev-parse HEAD)"
test -z "$(git status --short)"
test "$accepted_trend_win_rate_sha" = "$(git rev-parse HEAD)"
```

再严格按 `README.md` 的“final acceptance result is PASS”步骤，从本 worktree执行：

```bash
scripts/install_daily_premarket_launchd.sh \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env \
  --trend-only --market all

scripts/install_dashboard_launchd.sh --mode stack \
  --repo-root "$PWD" \
  --runtime-root /Users/ray/projects/open_trader \
  --python /Users/ray/projects/open_trader/.venv/bin/python
```

随后核验：

```bash
jq '{pid,working_directory,git_sha,heartbeat_at,blocker}' \
  /Users/ray/projects/open_trader/data/trend_controller/CN/status.json \
  /Users/ray/projects/open_trader/data/trend_controller/HK/status.json \
  /Users/ray/projects/open_trader/data/trend_controller/US/status.json
lsof -nP -iTCP:8766 -sTCP:LISTEN
curl --fail --silent --show-error -o /dev/null -w '%{http_code}\n' \
  http://127.0.0.1:8766/
```

Expected: 三个新 PID 存活，`working_directory` 是本 worktree，`git_sha` 全等于 `$accepted_trend_win_rate_sha`，heartbeat 前进，blocker 为空，review URL 返回 `200`。检查安装完成后的新 Controller、Gateway 和 Legacy 日志；不得停止或重启不属于本次 Dashboard 交付的生产 Prediction Service `8769`。

---

## Execution Routing

本项目的 `AGENTS.md` 已固定执行方式，不提供 main agent inline implementation：

1. 用户明确批准实施后，main 把本计划完整交给一个 `worker`；main 不写代码。
2. `worker` 按 Task 1-4 的 RED → GREEN → commit 顺序实施并提供命令输出。
3. main 在聚焦验证后派 `reviewer`；finding 回到原 `worker`，再由 `reviewer` 复审。
4. reviewer 清零后才运行最终 `make acceptance` 和 exact-SHA redeploy。
