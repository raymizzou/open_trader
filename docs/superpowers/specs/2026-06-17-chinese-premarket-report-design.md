# Chinese Premarket Report Design

## Goal

Make `reports/premarket/<YYYY-MM-DD>.md` fully Chinese and easier to understand.
The report should explain not only each symbol's importance level, but also why
the item matters before market open.

## Decisions

- Keep CSV outputs unchanged for automation compatibility.
- Localize only the Markdown premarket report and the classifier prompt.
- Do not add a second model call or translation layer.
- The report template must not display raw English enum values such as
  `high`, `action_changed`, or `reduce`.
- Free-text fields produced by the classifier must be requested in Chinese:
  `suggested_action`, `summary`, `rationale`, and `watch_trigger`.
- Each detailed symbol section must include a visible `为什么重要` paragraph from
  `rationale`.

## Report Shape

```markdown
# 开盘前交易简报 - 2026-06-16

## 今日需要关注

| 标的 | 重要性 | 当前仓位 | 建议动作 |
| --- | --- | --- | --- |
| AAPL | 高 | 5.10% | 减仓 |
| MSFT | 中 | 7.00% | 观察 |

## 详细说明

### 1. AAPL

| 项目 | 内容 |
| --- | --- |
| 重要性 | 高 |
| 当前仓位 | 5.10% |
| 变化类型 | 建议动作变化 |
| 建议动作 | 减仓 |

**为什么重要：** 今天的建议相对上次发生变化，且当前仓位较高，需要优先确认是否降低风险敞口。

**摘要：** 建议开盘前重点复核 AAPL 的仓位和风险。

**观察条件：** 若开盘后跌破计划止损位，应优先处理。
```

## Empty States

No material changes:

```markdown
# 开盘前交易简报 - 2026-06-16

今日没有需要特别关注的交易建议变化。
```

No eligible symbols:

```markdown
# 开盘前交易简报 - 2026-06-16

没有找到符合条件的美股或 ETF 标的。
```

## Enum Localization

Severity:

- `high`: `高`
- `medium`: `中`
- `low`: `低`

Change type:

- `new_signal`: `新信号`
- `action_changed`: `建议动作变化`
- `risk_changed`: `风险变化`
- `trigger_changed`: `触发条件变化`
- `no_material_change`: `无实质变化`

Suggested action should prefer Chinese classifier output. As a defensive fallback,
common English phrases are localized:

- `hold`: `持有`
- `watch`: `观察`
- `reduce`: `减仓`
- `add`: `加仓`
- `exit`: `清仓`
- `trim`: `减仓`
- `buy`: `买入`
- `sell`: `卖出`

Unknown values are shown as-is so the report never hides model output.

## Scope

This change does not alter portfolio parsing, model adapters, classification
schema, CSV files, trading plans, notifications, or action trigger behavior.
