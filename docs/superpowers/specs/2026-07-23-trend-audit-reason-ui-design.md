# 趋势报告候选排除原因 UI 设计

## 目标

让用户不必反查策略文档，就能直接理解每只候选为何未进入买入名单。
排除项默认显示“实际值 → 策略要求”，通过项保留在可展开的完整审计数据中。

本设计只改变 A 股趋势报告的 Dashboard 投影与展示，不改变报告哈希、候选判断、
风险控制或交易执行。

## 已确认方案

用户在交互原型中选择 **B：紧凑对照表**。

原型问题是“怎样让长候选名单的排除原因既明确又便于快速扫描”。选择 B 的结果：

- 桌面端用一行一只标的的对照表。
- “未通过项目”单元格逐项显示实际值和策略要求。
- 手机端同一语义结构转换为纵向卡片，不出现横向页面滚动。
- 原型保存在 throwaway 分支 `prototype/trend-audit-reason-ui`，
  提交 `567bba2`。

## 当前问题

现有 `renderCnTrendAudit()` 把结论、原因和全部指标拼进一条长文本：

```text
600671 天目药业｜结论 已排除｜排除原因 行业温度未达到热或沸、……
```

用户只能看到规则标签，不知道：

- 实际行业温度是“平”；
- 实际市值是 20 亿元；
- 实际日成交额是 1 亿元；
- 分别需要达到什么条件。

同一批排除项还会在“完整候选审计”和“排除项”中重复展示。

## 信息层级

审计详情保持默认折叠。展开后依次显示：

1. 标题：“为什么没有进入买入名单”。
2. 汇总：候选数、通过数、排除数，以及按原因统计的数量。
3. 候选对照表。
4. 行业集中度、数据来源和 API 成本。

删除重复的独立“排除项”列表。

## 桌面表格

表格保持报告中的候选顺序，不在前端重新排名。列为：

| 列 | 内容 |
|---|---|
| 标的 | 股票代码、名称、行业 |
| 结论 | `通过纪律`、`已排除 · N 项未通过`、`数据缺失`或`待确认` |
| 未通过项目 | 每项显示字段、实际值、箭头和策略要求 |
| 已通过的关键事实 | 温度变化、强度、节气、危险信号 |
| 审计 | 原生 `details/summary`，展开全部候选字段 |

示例：

```text
行业温度
平 → 要求：热或沸

总市值
20 亿元 → 要求：至少 100 亿元
```

颜色用于增强层级，不单独表达结论；所有状态都有文字。

## 手机布局

在 `760px` 以下：

- 隐藏表头。
- 每个表格行转换为独立卡片。
- 单元格使用 `data-label` 显示列名。
- 原因逐项纵向排列。
- 页面不得产生横向滚动。
- `summary` 的点击区域至少 44px 高。

## 数据来源

候选实际值已经存在于 `audit.candidates`，无需新增选股计算。

Dashboard 投影在 `audit` 中追加当前冻结报告的只读策略参数：

```json
{
  "audit": {
    "strategy_parameters": {
      "max_filter_price": "200",
      "min_strength": "95",
      "allowed_industry_temperatures": ["热", "沸"],
      "allowed_phases": ["谷雨", "立夏", "夏至"],
      "min_market_cap_100m": "100",
      "min_amount_100m": "2"
    }
  }
}
```

参数直接来自同一冻结报告的 `strategy_snapshot.parameters`。前端不得复制一套固定
阈值，以免历史报告或后续策略版本显示错误要求。

## 原因解释

前端增加一个纯渲染映射，根据原因代码读取候选实际值和冻结策略参数。它只解释
报告已经给出的排除原因，不重新判断候选是否合格。

`renderCnTrendAudit()` 同时接收当前报告投影，用 `report.data_date` 解释
`data_date_mismatch`；其他要求值来自 `audit.strategy_parameters`。

当前 A 股原因至少覆盖：

| 原因代码 | 实际值 | 策略要求 |
|---|---|---|
| `a_share_only` | `asset` | 仅限 A 股股票 |
| `temperature_missing` | 数据未提供 | 个股温度必须存在 |
| `temperature_transition_not_entry` | `temperature_prev → temperature_curr` | 温转热或温转沸 |
| `filter_price_missing` | 数据未提供 | 筛选价必须存在 |
| `filter_price_above_200` | `filter_price` | 不高于 `max_filter_price` 元 |
| `strength_missing` | 数据未提供 | 趋势强度必须存在 |
| `strength_below_95` | `strength` | 不低于 `min_strength` |
| `industry_id_missing` | 数据未提供 | 行业 ID 必须存在 |
| `industry_temperature_missing` | 数据未提供 | 行业温度必须存在 |
| `industry_temperature_not_hot` | `industry_temperature` | 属于 `allowed_industry_temperatures` |
| `phase_missing` | 数据未提供 | 趋势节气必须存在 |
| `phase_after_summer_solstice` | `phase` | 属于 `allowed_phases` |
| `market_cap_missing` | 数据未提供 | 总市值必须存在 |
| `market_cap_below_100` | `market_cap` | 至少 `min_market_cap_100m` 亿元 |
| `amount_missing` | 数据未提供 | 日成交额必须存在 |
| `amount_below_2` | `amount` | 至少 `min_amount_100m` 亿元 |
| `right_side_days_missing` | 数据未提供 | 右侧天数必须存在 |
| `right_side_not_true` | 未进入右侧 | 必须处于右侧趋势 |
| `not_tradable` | 当前不可交易 | 必须可交易 |
| `danger_unknown` | 数据未提供 | 危险信号必须明确 |
| `name_missing` | 数据未提供 | 标的名称必须存在 |
| `asset_missing` | 数据未提供 | 资产类型必须存在 |
| `unsupported_asset` | `asset` | A 股股票 |
| `already_held` | 当前已持有 | 新开仓候选不得已持有 |
| `excluded_security` | `name` / `exchange` | 非北交所、ST 或退市标的 |
| `unsupported_exchange` | `exchange` | 沪深市场 |
| `data_date_mismatch` | `as_of_date` | 与 `report.data_date` 一致 |

历史报告若包含 `atr_unavailable`，显示：

```text
ATR14：数据未提供 → 该历史策略版本要求 ATR14
```

当前 v5 不会产生这一排除原因。

未知原因不得降级成笼统的“未知原因”。显示：

```text
未识别规则：<原始原因代码> → 请核对冻结报告
```

所有文本继续经过现有 `escapeHtml()`。

## 值格式

- `danger: false` 显示“未触发”，`true` 显示“已触发”。
- `null`、空字符串和缺失字段显示“数据未提供”。
- 排名为空显示“未进入候选排名”。
- 筛选价和执行参考价使用“元”。
- 市值和成交额使用“亿元”。
- 数字使用现有 `formatDisplayNumber()`，避免输出过长小数。

## 错误处理

- `audit.candidates` 不是数组时显示“无候选审计数据”。
- `strategy_parameters` 缺失时仍显示实际值；要求显示“冻结策略参数未提供”。
- 单个原因代码或字段异常不得阻止其他候选渲染。
- 不根据当前代码参数补写历史报告阈值。

## 测试与验收

实现采用测试先行：

1. Dashboard 投影测试证明 `strategy_parameters` 来自冻结报告。
2. 前端渲染测试证明行业温度、市值和成交额显示实际值与要求值。
3. 缺失值测试证明显示“数据未提供”，不显示 JavaScript 的 `null`、`false`
   或笼统“未知原因”。
4. 未知原因代码测试证明原始代码被安全显示。
5. XSS 测试证明候选值、参数值和原因代码都经过转义。
6. 桌面浏览器验证表格列和原生展开详情。
7. 375px 浏览器验证卡片布局、44px 点击区域且无横向页面滚动。
8. 最终运行 `make acceptance`。只有结果为 `PASS` 才可交付。
9. 验收后重新部署完全相同的 Git SHA，并验证 PID、工作目录、Git SHA、
   新日志和 review URL HTTP 200。

## 非目标

- 不修改候选过滤条件。
- 不修改 ATR、行情补全或交易执行规则。
- 不新增筛选、排序、搜索、分页或导出功能。
- 不引入前端框架、组件库或依赖。
- 不把原型代码直接复制到生产实现。
