# 趋势报告候选排除原因 UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 A 股趋势报告“完整候选审计”改成用户确认的 B 版紧凑对照表，让每个排除原因直接显示“实际值 → 冻结策略要求”，并在 375px 手机端转换为无横向滚动的卡片。

**Architecture:** 冻结报告继续是唯一事实源。Python Dashboard 投影只把同一份 `strategy_snapshot.parameters` 复制到 `audit.strategy_parameters`；原生 JavaScript 根据报告已有的 `excluded_reasons` 生成解释，不重新计算候选资格；CSS 仅为 A 股审计表增加桌面表格和移动卡片规则。美股、港股、报告哈希、选股规则、ATR/行情补全和交易执行保持不变。

**Tech Stack:** Python 3.12、原生 JavaScript、CSS、pytest、现有 Playwright Dashboard acceptance、`screen`、`launchctl`

## Global Constraints

- 按仓库 `AGENTS.md` 执行：生产实现必须在从本地 `main` 新建的独立分支和 worktree 中完成。
- 实现 worktree 使用 `/Users/ray/projects/open_trader/.worktrees/trend-audit-reason-ui-implementation`，分支使用 `feat/trend-audit-reason-ui`。
- 不把 throwaway 原型 `trend-audit-reason-prototype.html` 带入实现分支。只从 `567bba2..prototype/trend-audit-reason-ui` cherry-pick 已批准的设计和本计划。
- 不改 A 股候选过滤条件、报告生成、ATR/行情获取、报告哈希、买入窗口、止盈止损或交易执行。
- `renderCnTrendAudit()` 只解释 `audit.candidates[*].excluded_reasons`，不得从候选数值重新推断通过/排除。
- 阈值来自当前打开报告的 `audit.strategy_parameters`；不得把 200 元、95、100 亿元、2 亿元等当前值写死到解释器。
- 历史 `atr_unavailable` 显示“该历史策略版本要求 ATR14”；不得把它恢复为当前规则。
- 未知原因显示原始原因代码；缺值显示“数据未提供”；缺冻结参数显示“冻结策略参数未提供”。
- 所有候选值、参数值和原因代码在进入 HTML 前必须经过 `escapeHtml()`。
- A 股删除重复的独立“排除项”区块；美股、港股原有审计区块不变。
- 不增加依赖、前端框架、图标库、筛选、搜索、排序、分页或导出。
- 每个行为改动先写失败测试，再写最小实现，再运行聚焦测试。
- 完成代码后先部署候选 SHA 到真实 Dashboard/三市场控制器，再运行最终 `make acceptance`。
- 只有 `make acceptance` 输出 `PASS` 才能交付。随后必须再次部署完全相同的 accepted SHA，并验证新 PID、cwd、SHA、新日志和 review URL HTTP 200。
- `CHANGELOG.md` 必须增加 2026-07-23 的 operator-facing 条目，满足 `main` 合并要求。

---

## File Map

- Modify: `src/open_trader/dashboard.py` — 投影冻结策略参数到 A 股审计数据。
- Modify: `tests/test_dashboard.py` — 证明投影来自同一份冻结报告，并对缺失/异常参数安全降级。
- Modify: `src/open_trader/dashboard_static/dashboard.js` — 原因解释器、汇总、B 版表格、完整字段详情。
- Modify: `src/open_trader/dashboard_static/dashboard.css` — 桌面紧凑表格和 760px 以下移动卡片。
- Modify: `tests/test_dashboard_web.py` — 实际值/要求值、历史 ATR、缺值、未知原因、XSS 和移动 CSS。
- Modify: `src/open_trader/dashboard_acceptance.py` — 真实浏览器检查 A 股审计结构、原因逐项解释和移动端无溢出。
- Modify: `tests/test_dashboard_acceptance.py` — acceptance helper 的确定性测试和文本预期。
- Modify: `CHANGELOG.md` — 记录候选审计可解释性改进及最终验证结果。

---

### Task 0: 建立干净的实现 worktree

**Files:**
- Read: `AGENTS.md`
- Carry forward: `docs/superpowers/specs/2026-07-23-trend-audit-reason-ui-design.md`
- Carry forward: `docs/superpowers/plans/2026-07-23-trend-audit-reason-ui.md`

- [ ] **Step 1: 确认本地 main 和现有工作区状态**

Run:

```bash
git -C /Users/ray/projects/open_trader status --short
git -C /Users/ray/projects/open_trader rev-parse main
git -C /Users/ray/projects/open_trader worktree list
```

Expected: 记录用户现有 dirty checkout，但不修改、不清理它；确认本地 `main` 的完整 SHA。

- [ ] **Step 2: 从本地 main 创建独立实现分支**

Run:

```bash
git -C /Users/ray/projects/open_trader worktree add \
  /Users/ray/projects/open_trader/.worktrees/trend-audit-reason-ui-implementation \
  -b feat/trend-audit-reason-ui main
```

Expected: 新 worktree 的 `HEAD` 等于 Step 1 记录的本地 `main` SHA。

- [ ] **Step 3: 只带入设计和计划，不带入原型**

Run:

```bash
git -C /Users/ray/projects/open_trader/.worktrees/trend-audit-reason-ui-implementation \
  cherry-pick 567bba2..prototype/trend-audit-reason-ui
git -C /Users/ray/projects/open_trader/.worktrees/trend-audit-reason-ui-implementation \
  ls-files src/open_trader/dashboard_static/trend-audit-reason-prototype.html
```

Expected: cherry-pick 成功；第二条命令无输出。

---

### Task 1: 投影同一冻结报告的策略参数

**Files:**
- Modify: `src/open_trader/dashboard.py:1530-1615`
- Test: `tests/test_dashboard.py:1254-1325,1360-1455`

**Interface:**

```json
{
  "audit": {
    "strategy_parameters": {
      "max_filter_price": "200",
      "min_strength": "95"
    }
  }
}
```

该字段只用于解释审计原因，不参与报告校验或候选判断。

- [ ] **Step 1: 写冻结参数投影的失败测试**

在 `tests/test_dashboard.py` 增加：

```python
def test_dashboard_projects_frozen_strategy_parameters_into_cn_audit(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    path = config.reports_dir / "trend_a_share/2026-07-15.json"
    path.parent.mkdir(parents=True)
    payload = _valid_v2_dashboard_trend_payload()
    snapshot = payload["strategy_snapshot"]
    assert isinstance(snapshot, dict)
    parameters = snapshot["parameters"]
    assert isinstance(parameters, dict)
    parameters.update({
        "max_filter_price": "200",
        "min_strength": "95",
        "allowed_industry_temperatures": ["热", "沸"],
        "allowed_phases": ["谷雨", "立夏", "夏至"],
        "min_market_cap_100m": "100",
        "min_amount_100m": "2",
    })
    path.write_text(json.dumps(payload), encoding="utf-8")

    report = dashboard_module._load_trend_reports(
        config.data_dir, config.reports_dir, today=date(2026, 7, 15)
    )["eastmoney"]

    assert report["available"] is True
    assert report["audit"]["strategy_parameters"] == parameters
    assert report["audit"]["strategy_parameters"] is not parameters
```

同时增加兼容测试：没有 `parameters` 或值不是 `dict` 的旧报告投影为 `{}`，不得让整个报告不可用。若严格报告校验不允许构造这种冻结产物，直接调用 `_project_broker_trend_report()` 的现有测试入口验证降级，不放宽报告校验。

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_dashboard.py::test_dashboard_projects_frozen_strategy_parameters_into_cn_audit
```

Expected: FAIL，`audit` 尚无 `strategy_parameters`。

- [ ] **Step 3: 增加最小只读投影**

在 `_project_broker_trend_report()` 形成返回值前读取快照：

```python
    strategy_snapshot = payload.get("strategy_snapshot")
    raw_strategy_parameters = (
        strategy_snapshot.get("parameters")
        if isinstance(strategy_snapshot, dict)
        else None
    )
    strategy_parameters = (
        dict(raw_strategy_parameters)
        if isinstance(raw_strategy_parameters, dict)
        else {}
    )
```

在现有 `audit` 中只增加一项：

```python
        "audit": {
            "candidates": audit_candidates,
            "strategy_parameters": strategy_parameters,
            "excluded": payload.get("excluded", {}),
            "account_exceptions": account.get("exceptions", []),
            "industry_concentration": payload.get("industry_concentration", []),
            "data_sources": payload.get("data_sources", []),
            "estimated_api_cost": payload.get("estimated_api_cost"),
            "actual_api_cost": payload.get("actual_api_cost"),
            "artifact": path.name,
        },
```

不要修改 `_report_hash()`、冻结 JSON 或候选列表。

- [ ] **Step 4: 运行聚焦回归并确认 GREEN**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_dashboard.py::test_dashboard_projects_frozen_strategy_parameters_into_cn_audit \
  tests/test_dashboard.py::test_dashboard_projects_frozen_risk_summary_and_skips \
  tests/test_dashboard.py::test_dashboard_projects_latest_same_day_trend_report_for_each_broker
```

Expected: 全部 PASS。

- [ ] **Step 5: 提交投影改动**

Run:

```bash
git add src/open_trader/dashboard.py tests/test_dashboard.py
git commit -m "feat: expose frozen trend audit parameters"
```

---

### Task 2: 增加纯前端原因解释器

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js:1920-1955,2581-2620`
- Test: `tests/test_dashboard_web.py:4686-4780,5090-5130`

**Data contract:**

```js
{
  label: "行业温度",
  actual: "平",
  requirement: "要求：热或沸",
  code: "industry_temperature_not_hot"
}
```

解释器返回纯文本，不返回 HTML，也不改变 `eligible`。

- [ ] **Step 1: 写实际值、冻结要求、缺值和未知原因的失败测试**

新增 `test_dashboard_cn_audit_explains_reported_reasons_with_frozen_requirements`，直接调用 `renderCnTrendAudit()`：

```js
const html = renderCnTrendAudit({
  strategy_parameters:{
    max_filter_price:"200",
    min_strength:"95",
    allowed_industry_temperatures:["热","沸"],
    allowed_phases:["谷雨","立夏","夏至"],
    min_market_cap_100m:"100",
    min_amount_100m:"2"
  },
  candidates:[
    {symbol:"600671",name:"天目药业",eligible:false,rank:null,
     excluded_reasons:[
       "industry_temperature_not_hot",
       "market_cap_below_100",
       "amount_below_2"
     ],
     industry_temperature:"平",market_cap:"20",amount:"1",danger:false},
    {symbol:"600236",name:"桂冠电力",eligible:false,
     excluded_reasons:["filter_price_missing","future_rule_<script>"]},
    {symbol:"600001",name:"通过样本",eligible:true,rank:1,
     excluded_reasons:[],temperature_prev:"温",temperature_curr:"热",
     strength:"99",phase:"立夏",danger:false}
  ],
  industry_concentration:[],
  data_sources:["Trend Animals"]
}, {data_date:"2026-07-22"});
for (const text of [
  "行业温度","平","要求：热或沸",
  "总市值","20 亿元","要求：至少 100 亿元",
  "日成交额","1 亿元","要求：至少 2 亿元",
  "筛选价","数据未提供","要求：筛选价必须存在",
  "未识别规则：future_rule_&lt;script&gt;","请核对冻结报告",
  "未触发"
]) {
  if (!html.includes(text)) throw new Error(text + "\n" + html);
}
for (const forbidden of ["未知原因", ">null<", ">false<"]) {
  if (html.includes(forbidden)) throw new Error(forbidden + "\n" + html);
}
```

新增历史和缺参数断言：

```js
const historical = renderCnTrendAudit({
  candidates:[{symbol:"600001",eligible:false,
    excluded_reasons:[
      "atr_unavailable","data_date_mismatch","strength_below_95"
    ],
    atr:null,as_of_date:"2026-07-21",strength:"94"}]
}, {data_date:"2026-07-22"});
for (const text of [
  "ATR14","数据未提供","该历史策略版本要求 ATR14",
  "数据日期","2026-07-21","要求：与报告数据日 2026-07-22 一致"
]) {
  if (!historical.includes(text)) throw new Error(text + "\n" + historical);
}
if (!historical.includes("冻结策略参数未提供")) {
  throw new Error(historical);
}
```

- [ ] **Step 2: 扩展现有 XSS 失败测试**

在 `test_dashboard_cn_trend_report_escapes_every_rendered_fact` 的 `audit` 中加入：

```js
strategy_parameters:{
  max_filter_price:attack,
  allowed_industry_temperatures:[attack]
}
```

让 `excluded_reasons` 同时包含已知原因和 `attack`。继续断言原始 `<img ...>` 不出现在 HTML，转义文本存在。

- [ ] **Step 3: 运行测试并确认 RED**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_dashboard_web.py::test_dashboard_cn_audit_explains_reported_reasons_with_frozen_requirements \
  tests/test_dashboard_web.py::test_dashboard_cn_trend_report_escapes_every_rendered_fact
```

Expected: FAIL，因为现有渲染器只显示静态原因标签。

- [ ] **Step 4: 实现值和冻结参数格式化 helper**

紧邻 `renderCnTrendAudit()` 添加纯文本 helper：

```js
function cnTrendAuditValue(value, suffix = "") {
  if (!hasValue(value)) return "数据未提供";
  if (typeof value === "boolean") return value ? "是" : "否";
  return `${formatDisplayNumber(value)}${suffix}`;
}

function cnTrendAuditDanger(value) {
  return value === true ? "已触发"
    : value === false ? "未触发"
    : "数据未提供";
}

function cnTrendAuditList(value) {
  return Array.isArray(value) && value.length
    ? value.map(formatPlain).join("或")
    : "";
}

function cnTrendAuditRequirement(parameters, key, render) {
  const value = parameters && typeof parameters === "object"
    ? parameters[key]
    : undefined;
  return hasValue(value) ? render(value) : "冻结策略参数未提供";
}
```

对数组使用 `cnTrendAuditList()`，不要依赖 `String(["热", "沸"])` 的逗号输出。

- [ ] **Step 5: 实现完整的原因映射**

增加 `cnTrendAuditReason(reason, item, parameters, report)`。以下代码必须全部有确定输出；已有冻结参数时取参数，没有时使用统一缺参文案：

```js
function cnTrendAuditReason(reason, item, parameters, report) {
  const temperature = `${cnTrendAuditValue(item.temperature_prev)} → ${cnTrendAuditValue(item.temperature_curr)}`;
  const rules = {
    a_share_only: ["资产类型", cnTrendAuditValue(item.asset), "要求：仅限 A 股股票"],
    temperature_missing: ["个股温度", "数据未提供", "要求：个股温度必须存在"],
    temperature_transition_not_entry: [
      "温度变化",
      temperature,
      cnTrendAuditRequirement(parameters, "temperature_transition", (value) => {
        const from = cnTrendAuditList(value && value.from);
        const to = cnTrendAuditList(value && value.to);
        return from && to ? `要求：${from} → ${to}` : "冻结策略参数未提供";
      }),
    ],
    filter_price_missing: ["筛选价", "数据未提供", "要求：筛选价必须存在"],
    filter_price_above_200: [
      "筛选价",
      cnTrendAuditValue(item.filter_price, " 元"),
      cnTrendAuditRequirement(parameters, "max_filter_price",
        (value) => `要求：不高于 ${formatDisplayNumber(value)} 元`),
    ],
    strength_missing: ["趋势强度", "数据未提供", "要求：趋势强度必须存在"],
    strength_below_95: [
      "趋势强度",
      cnTrendAuditValue(item.strength),
      cnTrendAuditRequirement(parameters, "min_strength",
        (value) => `要求：不低于 ${formatDisplayNumber(value)}`),
    ],
    industry_id_missing: ["行业 ID", "数据未提供", "要求：行业 ID 必须存在"],
    industry_temperature_missing: ["行业温度", "数据未提供", "要求：行业温度必须存在"],
    industry_temperature_not_hot: [
      "行业温度",
      cnTrendAuditValue(item.industry_temperature),
      cnTrendAuditRequirement(parameters, "allowed_industry_temperatures",
        (value) => `要求：${cnTrendAuditList(value) || "冻结策略参数未提供"}`),
    ],
    phase_missing: ["趋势节气", "数据未提供", "要求：趋势节气必须存在"],
    phase_after_summer_solstice: [
      "趋势节气",
      cnTrendAuditValue(item.phase),
      cnTrendAuditRequirement(parameters, "allowed_phases",
        (value) => `要求：${cnTrendAuditList(value) || "冻结策略参数未提供"}`),
    ],
    market_cap_missing: ["总市值", "数据未提供", "要求：总市值必须存在"],
    market_cap_below_100: [
      "总市值",
      cnTrendAuditValue(item.market_cap, " 亿元"),
      cnTrendAuditRequirement(parameters, "min_market_cap_100m",
        (value) => `要求：至少 ${formatDisplayNumber(value)} 亿元`),
    ],
    amount_missing: ["日成交额", "数据未提供", "要求：日成交额必须存在"],
    amount_below_2: [
      "日成交额",
      cnTrendAuditValue(item.amount, " 亿元"),
      cnTrendAuditRequirement(parameters, "min_amount_100m",
        (value) => `要求：至少 ${formatDisplayNumber(value)} 亿元`),
    ],
    right_side_days_missing: ["右侧天数", "数据未提供", "要求：右侧天数必须存在"],
    right_side_not_true: ["右侧趋势", "未进入右侧", "要求：必须处于右侧趋势"],
    not_tradable: ["交易状态", "当前不可交易", "要求：必须可交易"],
    danger_signal: ["危险信号", cnTrendAuditDanger(item.danger), "要求：不得触发"],
    danger_unknown: ["危险信号", "数据未提供", "要求：危险信号必须明确"],
    name_missing: ["标的名称", "数据未提供", "要求：标的名称必须存在"],
    asset_missing: ["资产类型", "数据未提供", "要求：资产类型必须存在"],
    unsupported_asset: ["资产类型", cnTrendAuditValue(item.asset), "要求：A 股股票"],
    already_held: ["账户状态", "当前已持有", "要求：新开仓候选不得已持有"],
    excluded_security: [
      "证券范围",
      [item.name, item.exchange].filter(hasValue).map(formatPlain).join(" / ") || "数据未提供",
      "要求：非北交所、ST 或退市标的",
    ],
    unsupported_exchange: ["交易所", cnTrendAuditValue(item.exchange), "要求：沪深市场"],
    atr_unavailable: ["ATR14", "数据未提供", "该历史策略版本要求 ATR14"],
    data_date_mismatch: [
      "数据日期",
      cnTrendAuditValue(item.as_of_date),
      hasValue(report && report.data_date)
        ? `要求：与报告数据日 ${formatPlain(report.data_date)} 一致`
        : "报告数据日未提供",
    ],
    amount_below_1: [
      "日成交额",
      cnTrendAuditValue(item.amount, " 亿元"),
      cnTrendAuditRequirement(parameters, "min_amount_100m",
        (value) => `要求：至少 ${formatDisplayNumber(value)} 亿元`),
    ],
    strength_not_above_90: [
      "趋势强度",
      cnTrendAuditValue(item.strength),
      cnTrendAuditRequirement(parameters, "min_strength",
        (value) => `要求：高于 ${formatDisplayNumber(value)}`),
    ],
    right_side_days_not_below_10: [
      "右侧天数",
      cnTrendAuditValue(item.days, " 天"),
      cnTrendAuditRequirement(parameters, "max_right_side_days_exclusive",
        (value) => `要求：少于 ${formatDisplayNumber(value)} 天`),
    ],
  };
  const values = rules[reason];
  return values
    ? {code: reason, label: values[0], actual: values[1], requirement: values[2]}
    : {
        code: formatPlain(reason),
        label: `未识别规则：${formatPlain(reason)}`,
        actual: "无法解析",
        requirement: "请核对冻结报告",
      };
}
```

注意：这里仅映射冻结报告已经给出的原因；即使某个数值看起来不合格，也不得自动追加原因。

- [ ] **Step 6: 运行聚焦测试并确认 GREEN**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_dashboard_web.py::test_dashboard_cn_audit_explains_reported_reasons_with_frozen_requirements \
  tests/test_dashboard_web.py::test_dashboard_cn_trend_report_escapes_every_rendered_fact
```

Expected: 全部 PASS。

- [ ] **Step 7: 提交解释器**

Run:

```bash
git add src/open_trader/dashboard_static/dashboard.js tests/test_dashboard_web.py
git commit -m "feat: explain frozen trend audit reasons"
```

---

### Task 3: 实现 B 版桌面表格和移动卡片

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js:2581-2620,2672`
- Modify: `src/open_trader/dashboard_static/dashboard.css:1660-1790,4380-4600`
- Modify: `tests/test_dashboard_web.py:4686-4780,5235-5270`

- [ ] **Step 1: 写 B 版结构的失败测试**

扩展 A 股报告渲染测试，要求：

```js
for (const text of [
  "为什么没有进入买入名单",
  "候选 3","通过 1","排除 2",
  "标的","结论","未通过项目","已通过的关键事实","审计",
  "已排除 · 3 项未通过","通过纪律","查看全部字段"
]) {
  if (!cn.includes(text)) throw new Error(text + "\n" + cn);
}
if ((cn.match(/class="trend-audit-row"/g) || []).length !== 3) throw new Error(cn);
if (!cn.includes('class="trend-audit-table"')) throw new Error(cn);
if (!cn.includes('class="trend-audit-reason"')) throw new Error(cn);
if (cn.includes("<h3>排除项</h3>")) throw new Error(cn);
```

增加坏输入测试：

```js
const empty = renderCnTrendAudit({candidates:{}}, {});
if (!empty.includes("无候选审计数据")) throw new Error(empty);
```

- [ ] **Step 2: 写移动 CSS 的失败测试**

在 `test_dashboard_trend_report_mobile_layout_css` 中验证：

```python
assert ".trend-audit-table {" in css
assert ".trend-audit-row" in mobile
assert ".trend-audit-table thead" in mobile
assert "display: none;" in mobile
assert "content: attr(data-label);" in mobile
assert ".trend-audit-reason" in mobile
assert "min-height: 44px;" in mobile
```

- [ ] **Step 3: 运行结构测试并确认 RED**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_dashboard_web.py::test_dashboard_renders_action_first_trend_report_for_every_market \
  tests/test_dashboard_web.py::test_dashboard_trend_report_mobile_layout_css
```

Expected: FAIL，因为旧版仍是长文本列表和重复“排除项”。

- [ ] **Step 4: 用单一候选数据生成汇总和表格**

把 `renderCnTrendAudit(audit)` 改为 `renderCnTrendAudit(audit, report)`，并把调用点改为：

```js
${isCn ? renderCnTrendAudit(audit, report) : renderTrendAudit(audit)}
```

新函数按以下结构生成：

```js
function renderCnTrendAudit(audit, report) {
  const candidates = cnTrendRows(audit.candidates);
  const parameters = audit.strategy_parameters
    && typeof audit.strategy_parameters === "object"
    && !Array.isArray(audit.strategy_parameters)
    ? audit.strategy_parameters : {};
  const industries = Array.isArray(audit.industry_concentration)
    ? audit.industry_concentration.filter(Array.isArray) : [];
  const dataSources = Array.isArray(audit.data_sources) ? audit.data_sources : [];
  const reasonCounts = new Map();

  const rows = candidates.map((item) => {
    const reasons = Array.isArray(item.excluded_reasons)
      ? item.excluded_reasons.map((reason) =>
          cnTrendAuditReason(reason, item, parameters, report))
      : [];
    reasons.forEach((reason) =>
      reasonCounts.set(reason.label, (reasonCounts.get(reason.label) || 0) + 1));
    const status = item.eligible === true
      ? {key:"passed", text:"通过纪律"}
      : item.eligible === false && reasons.length
        ? {key:"excluded", text:`已排除 · ${reasons.length} 项未通过`}
        : item.eligible === false
          ? {key:"missing", text:"数据缺失"}
          : {key:"review", text:"待确认"};
    const failed = reasons.length
      ? reasons.map((reason) => `<div class="trend-audit-reason">
          <strong>${escapeHtml(reason.label)}</strong>
          <span>${escapeHtml(reason.actual)} → ${escapeHtml(reason.requirement)}</span>
        </div>`).join("")
      : '<span class="trend-audit-none">无</span>';
    const facts = [
      `温度 ${cnTrendTemperature(item)}`,
      `强度 ${cnTrendAuditValue(item.strength)}`,
      `节气 ${cnTrendAuditValue(item.phase)}`,
      `危险信号 ${cnTrendAuditDanger(item.danger)}`,
    ].map((fact) => `<span>${escapeHtml(fact)}</span>`).join("");
    const details = Object.entries(item).map(([key, value]) => {
      const display = Array.isArray(value)
        ? value.map(formatPlain).join("、")
        : value && typeof value === "object"
          ? JSON.stringify(value)
          : cnTrendAuditValue(value);
      return `<div><dt>${escapeHtml(key)}</dt><dd>${escapeHtml(display)}</dd></div>`;
    }).join("");
    return `<tr class="trend-audit-row">
      <td data-label="标的"><strong>${escapeHtml(cnTrendIdentity(item))}</strong><span>${escapeHtml(formatPlain(item.industry))}</span></td>
      <td data-label="结论"><span class="trend-audit-status" data-status="${status.key}">${escapeHtml(status.text)}</span></td>
      <td data-label="未通过项目"><div class="trend-audit-reasons">${failed}</div></td>
      <td data-label="已通过的关键事实"><div class="trend-audit-facts">${facts}</div></td>
      <td data-label="审计"><details class="trend-audit-more"><summary>查看全部字段</summary><dl>${details}</dl></details></td>
    </tr>`;
  }).join("");
```

同一函数后半段计算：

```js
  const passed = candidates.filter((item) => item.eligible === true).length;
  const excluded = candidates.filter((item) => item.eligible === false).length;
  const reasonSummary = [...reasonCounts.entries()]
    .map(([label, count]) => `<span>${escapeHtml(label)} ${count}</span>`).join("");
```

最终 HTML 顺序必须是：

1. `<summary>审计详情</summary>`
2. `<section>` 内标题“为什么没有进入买入名单”
3. 候选/通过/排除和原因计数
4. 五列表格；无候选时显示“无候选审计数据”
5. 行业集中度
6. 数据来源和 API 成本

不要再读取或渲染 `audit.excluded`，避免重复列表。

- [ ] **Step 5: 增加最小桌面样式**

沿用现有 token：

```css
.trend-audit-summary,
.trend-audit-reason-counts,
.trend-audit-facts {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.trend-audit-table {
  border-collapse: collapse;
  table-layout: fixed;
  width: 100%;
}

.trend-audit-table th,
.trend-audit-table td {
  border-bottom: 1px solid var(--line);
  overflow-wrap: anywhere;
  padding: 10px;
  text-align: left;
  vertical-align: top;
}

.trend-audit-table th:nth-child(1) { width: 14%; }
.trend-audit-table th:nth-child(2) { width: 13%; }
.trend-audit-table th:nth-child(3) { width: 32%; }
.trend-audit-table th:nth-child(4) { width: 25%; }
.trend-audit-table th:nth-child(5) { width: 16%; }

.trend-audit-reasons,
.trend-audit-reason,
.trend-audit-more dl {
  display: grid;
  gap: 6px;
}

.trend-audit-reason {
  border-left: 3px solid var(--warning);
  padding-left: 8px;
}
```

状态颜色只能增强文字状态，不能替代“通过纪律/已排除/数据缺失/待确认”。

- [ ] **Step 6: 增加 760px 以下卡片样式**

在现有 `@media (max-width: 760px)` 中加入：

```css
  .trend-audit-table {
    display: block;
    max-width: 100%;
  }

  .trend-audit-table thead {
    display: none;
  }

  .trend-audit-table tbody {
    display: grid;
    gap: 12px;
  }

  .trend-audit-row {
    border: 1px solid var(--line);
    border-radius: 8px;
    display: grid;
    overflow: hidden;
  }

  .trend-audit-row td {
    border-bottom: 1px solid var(--line);
    display: grid;
    gap: 6px;
    min-width: 0;
    padding: 10px;
  }

  .trend-audit-row td::before {
    color: var(--muted);
    content: attr(data-label);
    font-size: 12px;
    font-weight: 700;
  }

  .trend-audit-more summary {
    align-items: center;
    display: flex;
    min-height: 44px;
  }
```

不得给整页或审计表增加横向滚动。

- [ ] **Step 7: 运行前端回归并确认 GREEN**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_dashboard_web.py::test_dashboard_renders_action_first_trend_report_for_every_market \
  tests/test_dashboard_web.py::test_dashboard_cn_audit_explains_reported_reasons_with_frozen_requirements \
  tests/test_dashboard_web.py::test_dashboard_cn_trend_report_escapes_every_rendered_fact \
  tests/test_dashboard_web.py::test_dashboard_trend_report_mobile_layout_css
```

Expected: 全部 PASS；US/HK 断言不变。

- [ ] **Step 8: 提交 B 版 UI**

Run:

```bash
git add \
  src/open_trader/dashboard_static/dashboard.js \
  src/open_trader/dashboard_static/dashboard.css \
  tests/test_dashboard_web.py
git commit -m "feat: redesign candidate audit as comparison table"
```

---

### Task 4: 把新审计结构加入真实 Dashboard acceptance

**Files:**
- Modify: `src/open_trader/dashboard_acceptance.py:2030-2085`
- Modify: `tests/test_dashboard_acceptance.py:1865-1905,2980-3020,3440-3525`

- [ ] **Step 1: 先更新 acceptance 单测并确认 RED**

东方财富的 `trend_audit_text()` / `trend_audit_sections()` 预期改为包含：

```text
审计详情
为什么没有进入买入名单
候选 1
通过 0
排除 1
600000 浦发银行
已排除 · 1 项未通过
趋势强度
94 → 要求：不低于 95
查看全部字段
行业集中度 无
```

并明确断言不再包含独立标题“排除项”。

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_dashboard_acceptance.py -k "trend_audit or mobile_report"
```

Expected: FAIL，因为 acceptance helper 仍假设旧版三个 A 股区块。

- [ ] **Step 2: 只为 eastmoney 检查新结构**

在 `_check_trend_audit()` 保留美股/港股旧分支；`broker == "eastmoney"` 时检查：

```python
table = audit.locator(".trend-audit-table")
assert table.count() == 1, "eastmoney 缺少候选审计表"
assert table.locator("thead th").all_inner_texts() == [
    "标的", "结论", "未通过项目", "已通过的关键事实", "审计",
]
rows = table.locator(".trend-audit-row")
assert rows.count() == len(candidates), "eastmoney 候选审计行数与 API 不一致"
assert audit.locator("section h3", has_text="排除项").count() == 0, (
    "eastmoney 仍重复显示排除项"
)
```

逐行验证：

- symbol/name 存在；
- `eligible` 对应明确中文结论；
- `.trend-audit-reason` 数量等于 `excluded_reasons` 数量；
- 每个原因文本同时包含 `→` 和“要求”或历史 ATR 文案；
- `.trend-audit-more` 存在且 summary 点击区域可用；
- 未知原因必须显示原始 code。

移动 viewport（`<= 760`）额外验证：

```python
assert audit.evaluate(
    "node => node.scrollWidth <= node.clientWidth"
), "eastmoney 移动候选审计横向溢出"
_check_mobile_targets(page, ".trend-audit-more summary")
```

如果 helper 只有 `audit` locator，没有 `page`，用 `audit.page` 或把 `page` 显式传入；不要用固定像素或测试桩绕过真实浏览器。

- [ ] **Step 3: 运行 acceptance helper 测试并确认 GREEN**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_dashboard_acceptance.py -k "trend_audit or mobile_report"
```

Expected: 全部选中测试 PASS。

- [ ] **Step 4: 运行所有相关自动化回归**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_dashboard.py \
  tests/test_dashboard_web.py \
  tests/test_dashboard_acceptance.py
```

Expected: 全部 PASS；记录准确通过数和耗时。

- [ ] **Step 5: 提交 acceptance 改动**

Run:

```bash
git add src/open_trader/dashboard_acceptance.py tests/test_dashboard_acceptance.py
git commit -m "test: accept explanatory trend audit layout"
```

---

### Task 5: Changelog、真实部署、最终 acceptance 和同 SHA 重部署

**Files:**
- Modify: `CHANGELOG.md`
- Runtime: `/tmp/open_trader_dashboard_8766.log`
- Runtime: `/Users/ray/projects/open_trader/data/trend_controller/{cn,hk,us}/status.json`

- [ ] **Step 1: 更新 changelog**

在 `CHANGELOG.md` 的 `## 2026-07-23` 顶部增加：

```markdown
- Reworked the A-share trend candidate audit into a desktop comparison table
  and mobile cards that show each reported exclusion's actual value against the
  frozen strategy requirement, retain historical ATR explanations, and expose
  unknown rule codes without changing candidate selection or execution.
```

该条目不预写测试数量、SHA 或 `PASS`，避免为了补验证结果而在最终 gate 后再次
修改源码。准确验证结果放在交付说明中。

- [ ] **Step 2: 提交 changelog 并确认工作区干净**

Run:

```bash
git add CHANGELOG.md
git commit -m "docs: update trend audit changelog"
git status --short
git log -1 --format='%H %s'
```

Expected: `git status --short` 无输出；记录候选完整 SHA。

- [ ] **Step 3: 部署候选 SHA 到三市场控制器和 Dashboard**

从实现 worktree 执行：

```bash
scripts/install_daily_premarket_launchd.sh \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env \
  --trend-only --market all

screen -S open_trader_dashboard_8766 -X quit 2>/dev/null || true
screen -dmS open_trader_dashboard_8766 zsh -lc \
  'cd /Users/ray/projects/open_trader/.worktrees/trend-audit-reason-ui-implementation && exec env PYTHONPATH=src .venv/bin/python -u -m open_trader dashboard --portfolio /Users/ray/projects/open_trader/data/latest/portfolio.csv --data-dir /Users/ray/projects/open_trader/data --reports-dir /Users/ray/projects/open_trader/reports --config /Users/ray/projects/open_trader/config/daily_premarket.env --poll-seconds 5 --host 127.0.0.1 --port 8766 >> /tmp/open_trader_dashboard_8766.log 2>&1'
```

Expected: 旧 Dashboard 和旧控制器不再运行；新进程均来自实现 worktree。

- [ ] **Step 4: 直接验证真实工作流**

Run:

```bash
screen -ls | rg 'open_trader_dashboard_8766'
pgrep -f 'open_trader dashboard' | xargs ps -o pid,lstart,command -p
launchctl list | rg 'com\\.open-trader\\.trend-market-controller\\.(cn|hk|us)'
tail -n 80 /tmp/open_trader_dashboard_8766.log
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/
curl -sS http://127.0.0.1:8766/api/dashboard | \
  .venv/bin/python -m json.tool >/dev/null
```

Expected:

- review URL 返回 `200`；
- API 是有效 JSON；
- Dashboard 日志首条 runtime 记录的 PID、cwd、Git SHA 是当前候选；
- 无 `Traceback` 或“看板数据加载失败”；
- 三个 controller status 的 PID 存活、cwd/SHA 正确且 heartbeat 推进。

- [ ] **Step 5: 运行最终唯一 acceptance gate**

Run:

```bash
make acceptance
```

Expected: 最后一行是 `PASS`。`FAIL` 必须继续修复并从相关 RED/GREEN 步骤重跑；`BLOCKED` 必须按外部阻塞报告，不能用 mock、curl、截图或单元测试替代。

- [ ] **Step 6: 验收后重部署完全相同的 accepted SHA**

不得再改文件或提交。先确认：

```bash
git status --short
git rev-parse HEAD
```

Expected: 工作区干净，`HEAD` 等于 Step 5 的 accepted SHA。

再次原样执行 Step 3 的控制器安装和 Dashboard 重启。

- [ ] **Step 7: 验证 post-acceptance review 部署**

检查：

```bash
screen -ls | rg 'open_trader_dashboard_8766'
pgrep -f 'open_trader dashboard' | xargs ps -o pid,lstart,command -p
tail -n 80 /tmp/open_trader_dashboard_8766.log
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/
curl -sS http://127.0.0.1:8766/api/dashboard | \
  .venv/bin/python -m json.tool >/dev/null
```

同时读取三份 controller status 两次，间隔不超过 60 秒，确认：

- 每个 PID 是重部署后的新 PID且存活；
- `working_directory` 是 implementation worktree；
- `git_sha` 等于 accepted SHA；
- `heartbeat_at` 在两次读取间推进；
- 新日志的 PID/SHA/cwd 与进程事实一致；
- Dashboard review URL `http://127.0.0.1:8766/` 返回 HTTP 200。

这是同一 accepted SHA 的纯重启，不需要第三次运行 acceptance。

---

## Completion Checklist

- [ ] 实现分支确实从执行时的本地 `main` 创建，用户原 dirty checkout 未被修改。
- [ ] throwaway mock HTML 未进入实现分支。
- [ ] `audit.strategy_parameters` 来自同一冻结报告，不影响报告哈希或候选资格。
- [ ] 原因解释器不重算规则，不硬编码当前阈值。
- [ ] 当前和历史原因、缺值、缺参数、未知 code、XSS 都有测试。
- [ ] A 股桌面是一行一标的的五列对照表。
- [ ] A 股移动端是同语义卡片，375px 无横向页面滚动，summary 至少 44px。
- [ ] 重复“排除项”只从 A 股 UI 删除；US/HK 无回归。
- [ ] 相关自动化测试 PASS。
- [ ] 真实 Dashboard、控制器、日志、API 和浏览器流程已检查。
- [ ] 最终 `make acceptance` 对最终 SHA 输出 `PASS`。
- [ ] 完全相同的 accepted SHA 已重部署，PID/cwd/SHA/日志/HTTP 200 已复核。
