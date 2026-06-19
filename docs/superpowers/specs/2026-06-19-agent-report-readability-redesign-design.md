# Agent Report Readability Redesign Design

## Goal

Redesign the dashboard symbol detail report area so the user can quickly answer:

- What should I do now?
- What should I watch today?
- Which analyst voice said what?
- What final conclusion came out of the analyst discussion?

The current detail view exposes separate `TradingAgents 报告`, `交易策略`,
`当前交易动作`, and broker detail sections. In practice, the first two sections
compete for attention, the long TradingAgents prose dominates the page, and the
trading instruction is not sufficiently visible. The redesign turns these into
one decision-first report area.

This is a read-only UI redesign. It must not run TradingAgents, call model APIs,
write trading artifacts, send notifications, or place broker orders.

## Approved Direction

Use direction A: `结论仪表盘 + 分析师对话`.

The detail page should present one primary area titled like:

```text
分析与交易策略
```

It replaces the current side-by-side emphasis on `TradingAgents 报告` and
`交易策略`. The page still uses the same underlying dashboard payload, but the
visual hierarchy changes:

1. First show the current desired action.
2. Then show the concrete operation details and today's watch points.
3. Then show analyst dialogue rows, each with a speaker role and concise point.
4. Then show the final conclusion.
5. Keep English source text collapsed behind a source-review entry.

## User Experience

The first viewport of a symbol detail page should contain a compact decision
dashboard.

Top header:

- Symbol and market, such as `US.DRAM`.
- Report date or generated date.
- Current view, such as `低配`.
- Report status, such as `正常` or `使用历史报告回退`.
- Read-only state, such as `只读 · 需要人工确认`.

Primary decision row:

- `当前希望你做什么`: one strong sentence such as `减仓 DRAM，先卖出约 50%`.
- `操作指令`: action, status, price or trigger price, quantity or sizing, stop
  condition.
- `今天重点关注`: the next one or two conditions the user should watch today.

Compact metrics row:

- `观点`
- `目标价`
- `触发状态`
- `动作状态`
- `下次复评`

These values should be short. Unknown values render as `-`, not `0`.

Analyst dialogue area:

- Label each row with a role, such as `趋势派`, `风控派`, `事件派`, `反方观点`,
  or `组合结论`.
- Each row should have a concise headline and one short explanation.
- The user should be able to understand the disagreement or reasoning flow
  without reading the raw TradingAgents paragraph.

Final conclusion area:

- `结论`: final view and action, such as `低配，但不是清仓`.
- `理由`: the main reason for the action.
- `条件`: what would change the conclusion.
- `失败条件`: the condition that invalidates the plan or forces review.

Source review:

- Keep `查看英文原文` or `查看原始报告` collapsed by default.
- Expanded raw text may be split into readable rows, but it must not be in the
  primary reading path.

## Section Consolidation

The current four-detail-section presentation should be adjusted as follows:

- `TradingAgents 报告`: no longer a large independent text block in the first
  viewport. Its rating, translated summary, raw decision, and source status feed
  the new decision dashboard, dialogue rows, conclusion, and collapsed source.
- `交易策略`: no longer a separate first-class peer to the report. Its target,
  stop, catalyst, horizon, plan text, and agent reason feed operation details,
  watch points, conclusion, and failure conditions.
- `当前交易动作`: becomes the leading decision source. If a current trade action
  exists, it drives the first decision row.
- `券商账户明细`: remains available but is moved below the report area or into a
  lower-priority collapsed/detail section, because it is not part of the analyst
  reasoning task.

If no current trade action exists, the decision row should show an explicit
non-action state, such as `今天暂无触发中的交易动作`, and still show available
analyst view and watch points.

## Data Sources

Use the existing dashboard payload built from local artifacts:

- `data/latest/portfolio.csv`
- `data/latest/trading_advice.csv`
- `data/latest/trading_plan.csv`
- `data/latest/premarket_actions.csv`
- `data/latest/trade_actions.csv`

Do not invent a second decision model in the frontend. The UI is a projection of
existing fields.

Preferred field precedence:

- Current action: `trade_action.action`, then `premarket_action.suggested_action`.
- Action status: `trade_action.status`, then `premarket_action.status`.
- Trigger: `trade_action.trigger_status`, then `premarket_action.watch_trigger`.
- Operation price: `trade_action.limit_price`, then `strategy.target_1`, then
  `strategy.target_range`.
- Quantity or sizing: `trade_action.suggested_quantity`,
  `trade_action.suggested_notional`, `strategy.max_weight`, or
  `strategy.target_weight`.
- Stop condition: `trade_action.stop_price`, then `strategy.stop_loss`.
- Analyst view: `strategy.rating`, `agent_report.rating`,
  `agent_report.advice_action`.
- Watch points: `trade_action.trigger_reason`, `premarket_action.watch_trigger`,
  `strategy.catalyst`, `strategy.time_horizon`, `strategy.plan_text`.
- Dialogue source text: `trade_action.agent_reason`, `strategy.agent_reason`,
  `agent_report.summary_zh`, `trade_action.agent_excerpt`,
  `strategy.agent_excerpt`, `agent_report.raw_decision`.

If an implementation discovers a needed field is missing from `/api/dashboard`,
the backend may pass through an existing CSV field directly. It should not infer
new financial advice beyond what the existing artifacts already contain.

## Dialogue Extraction

The frontend can format existing rationale text into dialogue rows. This is
display formatting only and must not write back to CSV or report files.

Rules:

- Prefer already-Chinese rationale fields when available.
- Suppress raw English prose from the Chinese primary UI unless it is inside the
  collapsed English source block.
- Split text on line breaks first.
- Split remaining long text on Chinese and English sentence punctuation.
- Group adjacent short sentences so the rows remain readable.
- Assign role labels using local keyword matching.
- Fall back to neutral labels if keywords are unclear.

Suggested role labels:

- `趋势派`: trend, technical, moving average, MACD, momentum, breakout,
  breakdown.
- `风控派`: risk, stop, drawdown, position sizing, exposure, volatility, review.
- `事件派`: earnings, policy, macro, rate, oil, geopolitical, catalyst.
- `反方观点`: explicit caveat, opposing view, upside, downside, uncertainty.
- `组合结论`: action, allocation, position, buy, trim, sell, hold, conclusion.

Rows should be concise and scannable. A long raw paragraph should never fill the
main detail column.

## Empty And Error States

Missing optional data should not break the detail page.

- Missing TradingAgents report: show `暂无 TradingAgents 报告`.
- Missing strategy: show `暂无交易策略`.
- Missing current action: show `今天暂无触发中的交易动作`.
- Missing broker detail: show `暂无券商账户明细` in the lower-priority broker area.
- Missing quote: show `缺行情`.
- Numeric unknown: show `-`.

Review and error states:

- `status=review` or `manual_review` should render as `需复核`.
- `status=error` should show the error in a visible warning.
- Missing quote or invalid sizing should remain review-only. The UI must not
  make an unsafe action look executable.
- `source_status=fallback` should render as `使用历史报告回退`.

## Frontend Design

Keep the current static frontend:

- `src/open_trader/dashboard_static/index.html`
- `src/open_trader/dashboard_static/dashboard.css`
- `src/open_trader/dashboard_static/dashboard.js`

Do not add React, Vite, a database, or a new build step.

Implementation shape:

- Replace the current independent `TradingAgents 报告` and `交易策略` first-view
  presentation with one combined `分析与交易策略` render function.
- Keep the existing symbol detail routing and back-to-list behavior.
- Keep the disabled `重新分析 · 未启用` button.
- Preserve mobile behavior by stacking decision cards, metrics, dialogue, and
  conclusion into one column.
- Keep the English source toggle behavior, but use clearer labels:
  `查看英文原文` or `查看原始报告`.

Visual style should match the existing dashboard: restrained, dense, and
operational. Do not add decorative hero sections, large marketing cards, or
unrelated visual ornament.

## Backend Design

No backend contract change is required if the current dashboard payload already
contains the needed fields.

If a missing field blocks the approved UI, extend `load_dashboard_state()` by
passing through that exact existing CSV field into the relevant section object.
The backend should not summarize, translate, classify, or generate new advice
for this redesign.

## Testing Strategy

Add focused tests without calling real Futu OpenD, TradingAgents, or model APIs.

Static frontend tests:

- Dashboard assets include `分析与交易策略`, `当前希望你做什么`, `今天重点关注`,
  `分析师对话`, `最终结论`, and `查看英文原文` or `查看原始报告`.
- The old first-view text-wall behavior is not reintroduced.
- The detail UI still contains `重新分析` and `未启用`.
- Known action, status, and trigger enums render as Chinese labels.
- Raw English prose is suppressed from the Chinese primary UI and remains
  available only in the collapsed source block.

Runtime helper tests, using Node if available:

- Dialogue extraction splits long rationale into multiple role-labeled rows.
- English primary rationale is suppressed when Chinese alternatives exist.
- Mixed Chinese text with business tokens, such as `MACD` or `OpenAI`, remains
  visible.
- Missing action renders the explicit no-action state.

Browser verification:

- Start the local dashboard.
- Open it with Playwright or the in-app browser.
- Select a symbol with TradingAgents and trade action data.
- Verify the first viewport shows the current desired action, operation
  details, and today watch points.
- Verify dialogue rows and final conclusion are visible without expanding
  English source.
- Expand and collapse the English source.
- Verify mobile width stacks cleanly and text does not overlap.

## Out Of Scope

- Running per-symbol reanalysis from the dashboard.
- Editing generated trading plans.
- Historical report browsing.
- Changing CSV schemas for trading actions or premarket actions.
- Sending notifications from the detail page.
- Order review or broker order submission.
- Model-based re-summarization of the report during dashboard rendering.
