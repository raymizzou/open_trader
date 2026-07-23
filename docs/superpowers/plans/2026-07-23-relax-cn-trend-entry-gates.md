# A 股趋势入场门槛放宽 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 A 股行业温度“温、热、沸”均可入场，删除 200 元筛选价硬门槛，保留 ATR14 等现有风险规则，并重跑 2026-07-23 报告供用户对比。

**Architecture:** 保留冻结的 `v1`–`v4` 策略快照原样可回放，只让当前 A 股实时快照升级到 `v5`；美股和港股继续使用 `v4`。候选判定只删除价格原因并放宽行业集合，现有风险、Kelly、回撤、排序和保护线代码不改算法。

**Tech Stack:** Python 3.12、pytest、原生 JavaScript、现有 launchd/screen 运维脚本。

## Global Constraints

- A 股 `v5` 从 `2026-07-24` 起生效；美股、港股继续使用 `v4`。
- `allowed_industry_temperatures` 必须为 `["温", "热", "沸"]`。
- 当前 A 股快照不得声明 `max_filter_price`，但旧报告的价格原因仍须可解释。
- 数据日期、非当前持仓、右侧天数和 ATR14 要求保持不变。
- 不改变仓位、Kelly、风险预算、保护线、退出规则或候选排序。
- 不新增依赖、配置项或抽象层。
- 只在全部实现和直接报告验证完成后运行一次最终 `make acceptance`。

---

### Task 1: 放宽候选门槛并生成 A 股 v5 快照

**Files:**
- Modify: `tests/test_a_share_trend.py`
- Modify: `tests/test_market_trend.py`
- Modify: `src/open_trader/a_share_trend.py`

**Interfaces:**
- Consumes: `CandidateInput`, `build_candidate_list(...)`, `trend_strategy_snapshot(...)`, `live_trend_strategy_snapshot(...)`.
- Produces: `CN_ALLOWED_INDUSTRY_TEMPERATURES`, A 股当前 `v5` 快照，以及仍可按显式版本构造的 `v4` 快照。

- [ ] **Step 1: 写候选门槛和快照失败测试**

在 `tests/test_a_share_trend.py` 添加并调整以下断言：

```python
@pytest.mark.parametrize("industry_temperature", ["温", "热", "沸"])
def test_cn_candidate_accepts_warm_or_hot_industry(
    industry_temperature: str,
) -> None:
    item = candidate("600001", industry_temperature=industry_temperature)
    assert build_candidate_list([item], held_symbols=set()).eligible == (item,)


@pytest.mark.parametrize("industry_temperature", ["凉", "平", None])
def test_cn_candidate_rejects_industry_below_warm_or_missing(
    industry_temperature: str | None,
) -> None:
    item = candidate("600001", industry_temperature=industry_temperature)
    decision = build_candidate_list([item], held_symbols=set())
    assert decision.eligible == ()


@pytest.mark.parametrize("filter_price", [None, "200.01", "1500"])
def test_cn_candidate_does_not_gate_on_filter_price(
    filter_price: str | None,
) -> None:
    item = candidate("600001", filter_price=filter_price)
    assert build_candidate_list([item], held_symbols=set()).eligible == (item,)


def test_live_cn_strategy_snapshot_is_v5_with_relaxed_entry_gates() -> None:
    snapshot = trend_module.live_trend_strategy_snapshot(
        "CN", "abc123", (622466, 697199)
    )
    assert snapshot["strategy_id"] == "trend_animals_warm_to_hot/CN/v5"
    assert snapshot["strategy_version"] == "v5"
    assert snapshot["effective_from"] == "2026-07-24"
    assert snapshot["parameters"]["allowed_industry_temperatures"] == [
        "温", "热", "沸"
    ]
    assert "max_filter_price" not in snapshot["parameters"]
    rows = {row["name"]: row["value"] for row in snapshot["parameter_rows"]}
    assert "筛选价格" not in rows
    assert rows["行业温度"] == "温、热或沸"
```

保留 `test_cn_strategy_snapshot_matches_runtime_rules_and_report_actions` 对基础 `v3` 旧形状的原断言，以证明旧快照未被重写。把现有 A 股报告运行器的当前版本断言从 `v4` 改为 `v5`；`tests/test_market_trend.py` 中 US/HK 当前版本仍断言 `v4`。

- [ ] **Step 2: 运行测试并确认失败**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_a_share_trend.py \
  tests/test_market_trend.py \
  -k 'candidate_accepts_warm or candidate_rejects_industry or does_not_gate_on_filter_price or strategy_snapshot' -q
```

Expected: FAIL；“温”行业仍被排除、价格缺失/高于 200 仍被排除、A 股当前快照仍为 `v4`。

- [ ] **Step 3: 最小修改候选判定**

在 `src/open_trader/a_share_trend.py` 增加一个集合并只在行业门槛与持仓提示复用：

```python
CN_ALLOWED_INDUSTRY_TEMPERATURES = {"温", "热", "沸"}
```

把 A 股 `_candidate_reasons(...)` 中的行业判断改为：

```python
if item.industry_temperature is None:
    reasons.append("industry_temperature_missing")
elif item.industry_temperature not in CN_ALLOWED_INDUSTRY_TEMPERATURES:
    reasons.append("industry_temperature_not_hot")
```

删除以下价格排除代码，不删除 `CandidateInput.filter_price` 或 API 读取：

```python
if item.filter_price is None:
    reasons.append("filter_price_missing")
elif item.filter_price > CN_MAX_FILTER_PRICE:
    reasons.append("filter_price_above_200")
```

把 `_holding_entry_hints(...)` 的行业文案改成“未达到温、热或沸”，并删除其中
“筛选价数据不可用／高于入场上限”的两个提示；候选/持仓表仍直接展示
`filter_price` 来源事实。把 `_REASON_LABELS["industry_temperature_not_hot"]`
改为通用的“行业温度未达到要求”，使新旧冻结参数都不会得到错误的静态文案。

- [ ] **Step 4: 让实时快照按市场选择 v5/v4**

保留 `trend_strategy_snapshot(...)` 的历史 `v3` 形状。给 `live_trend_strategy_snapshot(...)` 增加可选的历史版本参数：

```python
def live_trend_strategy_snapshot(
    market: str,
    process_version: str,
    candidate_pool_ids: Sequence[int],
    *,
    normal_cost_rate: Decimal = NORMAL_COST_RATE,
    strategy_version: str | None = None,
) -> dict[str, object]:
    market = market.upper()
    version = strategy_version or ("v5" if market == "CN" else "v4")
    if version not in {"v4", "v5"} or version == "v5" and market != "CN":
        raise ValueError("unsupported live trend strategy version")
```

对 `version == "v5"` 的参数和参数行做最小投影：

```python
if version == "v5":
    parameters.pop("max_filter_price", None)
    parameters["allowed_industry_temperatures"] = ["温", "热", "沸"]
    rows = [
        dict(row)
        for row in snapshot["parameter_rows"]
        if row["name"] != "筛选价格"
    ]
    for row in rows:
        if row["name"] == "行业温度":
            row["value"] = "温、热或沸"
else:
    rows = [dict(row) for row in snapshot["parameter_rows"]]
```

返回的 `strategy_id`、`strategy_version` 和 `effective_from` 使用：

```python
"strategy_id": f"trend_animals_warm_to_hot/{market}/{version}",
"strategy_version": version,
"effective_from": "2026-07-24" if version == "v5" else "2026-07-20",
```

更新 `_expected_report_strategy_snapshot(...)`：请求 `v4` 或 `v5` 时都调用
`live_trend_strategy_snapshot(..., strategy_version=requested_version)`。把
`a_share_trend.py` 内风险、Kelly、回撤和报告验证中的版本集合扩为包含 `v5`；
回撤校验的 `expected_strategy_version` 使用当前 `version`，不写死 `v4`。

测试辅助函数 `unlock_live_drawdown(...)` 接收可选 `strategy_version`，默认 CN 使用
`v5`；专门验证历史 `v4` 的测试显式传 `strategy_version="v4"`。当前回撤暂停测试的
`strategy_id`、`strategy_version` 和 `kelly_sample_key` 全部改为同一个 v5 身份，
避免用 v4 状态验证 v5 报告。

- [ ] **Step 5: 运行 Task 1 测试**

Run:

```bash
.venv/bin/python -m pytest tests/test_a_share_trend.py tests/test_market_trend.py -q
```

Expected: PASS。

- [ ] **Step 6: 提交候选与快照改动**

```bash
git add src/open_trader/a_share_trend.py tests/test_a_share_trend.py tests/test_market_trend.py
git commit -m "feat: relax CN trend entry gates"
```

---

### Task 2: 让回放和 Dashboard 接受 A 股 v5

**Files:**
- Modify: `src/open_trader/trend_review.py`
- Modify: `src/open_trader/dashboard.py`
- Modify: `src/open_trader/dashboard_acceptance.py`
- Modify: `tests/test_trend_review.py`
- Modify: `tests/test_dashboard.py`
- Modify: `tests/test_dashboard_acceptance.py`

**Interfaces:**
- Consumes: Task 1 的 `live_trend_strategy_snapshot(..., strategy_version=...)` 和现有 `valid_v4_risk_contract(...)`。
- Produces: v5 风险/回放/回撤验证；v4 历史报告和 US/HK 当前报告保持可用。

- [ ] **Step 1: 写 v4 历史兼容和 v5 投影失败测试**

在 `tests/test_trend_review.py` 增加：

```python
def test_cn_v4_and_v5_snapshots_normalize_without_cross_version_rewrite() -> None:
    old = live_trend_strategy_snapshot(
        "CN", "abc123", (622466, 697199), strategy_version="v4"
    )
    current = live_trend_strategy_snapshot(
        "CN", "abc123", (622466, 697199)
    )
    assert normalize_trend_strategy_snapshot(old, "CN") == old
    assert normalize_trend_strategy_snapshot(current, "CN") == current
    assert old["parameters"]["max_filter_price"] == "200"
    assert "max_filter_price" not in current["parameters"]
```

把重试风险上限参数化版本改为：

```python
@pytest.mark.parametrize("strategy_version", ["v2", "v3", "v4", "v5"])
```

在 Dashboard 测试构造一个 CN `v5` payload，断言 `_valid_trend_risk_summary(...)` 接受它，同时继续接受已有 `v4` fixture。

- [ ] **Step 2: 运行测试并确认失败**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_trend_review.py \
  tests/test_dashboard.py \
  tests/test_dashboard_acceptance.py \
  -k 'v5 or cross_version or risk_cap' -q
```

Expected: FAIL；版本白名单和写死的 `v4` 回撤身份尚未接受 A 股 `v5`。

- [ ] **Step 3: 扩展回放版本集合**

在 `src/open_trader/trend_review.py`：

- `normalize_trend_strategy_snapshot(...)` 对 `v4`/`v5` 都显式按该版本构造期望快照；
- 风险成交、rebuild 必填字段、Kelly 输入和 drawdown 输入的版本集合加入 `v5`；
- `drawdown_summary` 对 `v4` 和 `v5` 均保留；
- 不改 `_legacy_strategy_snapshot_variants(...)` 的旧报告内容。

核心选择保持：

```python
if snapshot.get("strategy_version") in {"v4", "v5"}:
    expected_snapshot = live_trend_strategy_snapshot(
        market,
        process_version,
        pools,
        strategy_version=str(snapshot["strategy_version"]),
    )
```

- [ ] **Step 4: 扩展 Dashboard 风险和验收身份**

在 `src/open_trader/dashboard.py` 将风险/Kelly/回撤版本集合加入 `v5`，并将：

```python
expected_strategy_version="v4"
```

改为：

```python
expected_strategy_version=strategy_version
```

在 `src/open_trader/dashboard_acceptance.py` 按 broker 选择当前版本：

```python
expected_version = "v5" if broker == "eastmoney" else "v4"
```

策略 ID、报告版本、回撤审计身份都使用 `expected_version`。不要放宽 US/HK 为 v5。
把验收错误文案中的“v4 策略身份”改为不绑定版本号的“策略身份”。保留
`filter_price_missing`、`filter_price_above_200` 和
`industry_temperature_not_hot` 的历史解释映射，但把最后一个静态摘要改为
“行业温度未达到要求”。

- [ ] **Step 5: 运行 Task 2 测试**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_trend_review.py \
  tests/test_dashboard.py \
  tests/test_dashboard_acceptance.py -q
```

Expected: PASS。

- [ ] **Step 6: 提交兼容层**

```bash
git add \
  src/open_trader/trend_review.py \
  src/open_trader/dashboard.py \
  src/open_trader/dashboard_acceptance.py \
  tests/test_trend_review.py \
  tests/test_dashboard.py \
  tests/test_dashboard_acceptance.py
git commit -m "feat: validate CN trend strategy v5"
```

---

### Task 3: 更新当前规则展示和操作文档

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `tests/test_dashboard_web.py`
- Modify: `纪律.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: 报告中的冻结 `strategy_snapshot.parameters`。
- Produces: 当前纪律文案；旧价格原因继续由现有 `cnTrendAuditReason(...)` 按历史参数解释。

- [ ] **Step 1: 写 Dashboard 文案失败测试**

在 `tests/test_dashboard_web.py` 对 `renderCnTrendDisciplines()` 增加：

```javascript
const html = renderCnTrendDisciplines();
for (const text of ["行业温度为温、热或沸", "强度不低于 95", "ATR 可用"]) {
  if (!html.includes(text)) throw new Error(text + "\n" + html);
}
for (const text of ["筛选价不高于 200 元", "行业温度为热或沸"]) {
  if (html.includes(text)) throw new Error(text + "\n" + html);
}
```

保留现有历史审计测试中：

```javascript
max_filter_price:"200",
allowed_industry_temperatures:["热","沸"]
```

以及“筛选价必须存在”的断言，证明历史报告仍按冻结规则解释。

- [ ] **Step 2: 运行测试并确认失败**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py -k 'discipline or audit' -q
```

Expected: FAIL；当前纪律仍显示价格上限和行业“热或沸”。

- [ ] **Step 3: 更新当前 Dashboard 和纪律文档**

把 `renderCnTrendDisciplines()` 的当前买入纪律改为：

```html
<li>趋势强度不低于 95</li>
<li>行业温度为温、热或沸，节气不晚于夏至</li>
```

保留候选表中的“筛选价（Trend Animals）”列和历史原因解释映射。

在 `纪律.md`：

- 删除“筛选价格不高于 200 元”硬门槛；
- 把行业温度改为“温、热或沸”；
- 保留日线和 ATR14 条目；
- 后续编号顺延，不改其他规则。

在 `CHANGELOG.md` 当日条目写明：

```markdown
- Relaxed the current CN trend entry gate to accept warm, hot, or boiling
  industries and removed the static CNY 200 filter-price cap; retained
  ATR14-based protection/risk sizing and historical snapshot rendering.
```

- [ ] **Step 4: 运行展示测试**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py tests/test_dashboard.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交展示与日志**

```bash
git add src/open_trader/dashboard_static/dashboard.js tests/test_dashboard_web.py 纪律.md CHANGELOG.md
git commit -m "docs: present relaxed CN trend entry rules"
```

---

### Task 4: 完整验证、重跑今日报告并部署复核版本

**Files:**
- Runtime output: `/Users/ray/projects/open_trader/reports/trend_a_share/2026-07-23-r*.json`
- Runtime output: `/Users/ray/projects/open_trader/data/trend_review/daily/CN/2026-07-23.json`
- Runtime logs: `/Users/ray/projects/open_trader/logs/daily_premarket/launchd-trend-controller-cn.*.log`
- Runtime log: `/tmp/open_trader_dashboard_8766.log`

**Interfaces:**
- Consumes: committed Task 1–3 SHA and shared live config/data directories.
- Produces: 2026-07-23 新版 A 股报告、旧版对比、最终 acceptance 结果和 review URL。

- [ ] **Step 1: 运行完整自动化测试**

Run:

```bash
make test
git diff --check
git status --short
```

Expected: `3349` 个基线测试加本次新增测试全部 PASS；`git diff --check` 静默；工作树干净。

- [ ] **Step 2: 记录候选 SHA 并部署它用于真实报告验证**

```bash
git rev-parse HEAD
scripts/install_daily_premarket_launchd.sh \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env \
  --trend-only --market CN
screen -S open_trader_dashboard_8766 -X quit 2>/dev/null || true
screen -dmS open_trader_dashboard_8766 zsh -lc \
  'cd /Users/ray/projects/open_trader/.worktrees/relax-cn-trend-entry-gates && exec env PYTHONPATH=src .venv/bin/python -u -m open_trader dashboard --portfolio /Users/ray/projects/open_trader/data/latest/portfolio.csv --data-dir /Users/ray/projects/open_trader/data --reports-dir /Users/ray/projects/open_trader/reports --config /Users/ray/projects/open_trader/config/daily_premarket.env --poll-seconds 5 --host 127.0.0.1 --port 8766 >> /tmp/open_trader_dashboard_8766.log 2>&1'
```

Expected: CN controller和 Dashboard 均从本 worktree 的候选 SHA 运行；旧 PID 已退出。

- [ ] **Step 3: 请求并等待 2026-07-23 报告修订**

```bash
PYTHONPATH=src .venv/bin/python -m open_trader trend-market run \
  --market CN --revision \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env
PYTHONPATH=src .venv/bin/python -m open_trader trend-market status \
  --market CN \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env
```

Expected: 控制器生成下一个不可变 `2026-07-23-rN.json`；其 `as_of_date` 为 `2026-07-23`、`execution_date` 为 `2026-07-24`、策略版本为 `v5`，且进程工作目录与 Git SHA 指向本 worktree。

- [ ] **Step 4: 对比旧报告与新报告**

用 `jq` 读取修订前的 `2026-07-23-r1.json` 和最新 `2026-07-23-rN.json`，输出：

```text
strategy_version
allowed_industry_temperatures
max_filter_price 是否存在
合格候选代码
正式 BUY 代码
excluded 数量
industry_temperature_not_hot 数量
filter_price_missing 数量
filter_price_above_200 数量
```

Expected: 新报告为 v5，允许温/热/沸，无 `max_filter_price`；价格原因计数为 0。把实际候选和买入变化保留给最终回复，不预设数量。

- [ ] **Step 5: 最终 acceptance 门禁**

Run:

```bash
make acceptance
```

Expected: `PASS`。`FAIL` 时继续修复并重跑；`BLOCKED` 时停止并报告环境阻塞，不能用 curl、fixture 或单元测试替代。

- [ ] **Step 6: acceptance 后重部署同一 SHA 并验证**

不再修改源码或数据，重新执行 Step 2 的 CN controller 和 Dashboard 部署命令，然后检查：

```bash
PYTHONPATH=src .venv/bin/python -m open_trader trend-market status \
  --market CN \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env
screen -ls | rg 'open_trader_dashboard_8766'
ps -axo pid,lstart,command | rg 'open_trader (dashboard|trend-market run)'
tail -n 80 /Users/ray/projects/open_trader/logs/daily_premarket/launchd-trend-controller-cn.{out,err}.log
tail -n 80 /tmp/open_trader_dashboard_8766.log
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/
curl -sS http://127.0.0.1:8766/api/dashboard | .venv/bin/python -m json.tool >/dev/null
```

Expected: 新 PID、正确工作目录、候选 Git SHA、新鲜日志、review URL HTTP 200、API 为有效 JSON。
