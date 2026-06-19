# Trade Report UI Redesign Design

## Goal

Redesign the dashboard trade report surfaces so a trading decision is clear at a
glance and the TradingAgents rationale is readable.

The redesign covers two existing dashboard surfaces:

- The right-rail `今日交易动作` list.
- The symbol detail `当前交易动作` and TradingAgents report sections.

This is a read-only UI change. It must not change the machine-readable trade
action CSV contract, run TradingAgents, call model APIs, write trading artifacts,
send notifications, or place orders.

## Selected UX

Use the approved A direction: decision-first detail view plus compact action
cards in the right rail.

The right rail becomes a quick review queue. Each action card shows:

- Market and symbol, such as `US.VIXY`.
- Chinese action label and status, such as `减仓 · 待确认`.
- Trigger status, such as `达到第一目标价`.
- Limit price, suggested quantity, suggested notional, and currency when known.
- A one-sentence Chinese trigger reason.
- A `查看完整策略` entry point that opens the existing symbol detail view.

The symbol detail view becomes the full trade report. It starts with a decision
band:

- `清晰交易策略`: the strategy conclusion in one or two short lines.
- `操作方向与价位`: action, status, limit price, quantity, notional, stop price.
- `简短触发理由`: a short Chinese explanation of why the action is active now.

Below the decision band, show trade impact metrics:

- Current quantity.
- Post-trade quantity.
- Suggested notional.
- Post-trade weight when available.
- Next trigger or review condition when available.

The long TradingAgents rationale is no longer shown as one large paragraph.
Render it as a `理由对话` section with short, separate rows. Each row has a role
label and a sentence-level point. Example labels:

- `趋势派`
- `风控派`
- `事件派`
- `组合结论`

The English source remains available behind `查看英文原文`, collapsed by default.
When expanded, it should also be split into readable paragraphs or dialogue rows
instead of a single text wall.

## Data Sources

Keep using the current dashboard payload built from local artifacts:

- `data/latest/portfolio.csv`
- `data/latest/trading_advice.csv`
- `data/latest/trading_plan.csv`
- `data/latest/premarket_actions.csv`
- `data/latest/trade_actions.csv`

Do not invent a second decision model in the UI. The UI is a human-readable
projection of the existing CSV rows and merged holding detail objects.

Primary fields:

- `trade_action.action`
- `trade_action.status`
- `trade_action.priority`
- `trade_action.trigger_status`
- `trade_action.limit_price`
- `trade_action.stop_price`
- `trade_action.suggested_quantity`
- `trade_action.suggested_notional`
- `trade_action.notional_currency`
- `trade_action.current_quantity`
- `trade_action.post_trade_quantity`
- `trade_action.post_trade_weight`
- `trade_action.reason`
- `trade_action.trigger_reason`
- `trade_action.agent_reason`
- `trade_action.agent_excerpt`
- `strategy.plan_text`
- `strategy.agent_reason`
- `strategy.agent_excerpt`
- `agent_report.summary_zh`
- `agent_report.raw_decision`

If the backend lacks a field, the frontend shows `-` or an explicit empty state.
Unknown numeric fields must not be rendered as zero.

## Frontend Design

Keep the current static frontend:

- `src/open_trader/dashboard_static/index.html`
- `src/open_trader/dashboard_static/dashboard.css`
- `src/open_trader/dashboard_static/dashboard.js`

Do not add React, Vite, a database, or a new build step.

### Right Rail

Replace the current field-light action item with a structured action card.

Card layout:

- Header: symbol, short source context, action/status pill.
- Price row: limit price, quantity, notional.
- Reason row: one sentence, Chinese, capped visually to avoid text walls.
- Footer action: `查看完整策略`.

Sorting should stay conservative and predictable:

- `ready` / `待确认` first.
- `review` / `需复核` second.
- `watch` / `观察` after executable or review rows.
- Existing priority should break ties.

The right rail summary shows counts for `待确认`, `复核`, and `观察`.

### Symbol Detail

Replace the current `当前交易动作` definition list with the decision band and
impact metrics.

The detail view keeps the existing `TradingAgents 报告`, `交易策略`, and
`券商账户明细` sections, but the decision band is the first trade-related
content visible after the overview metrics.

Rendering rules:

- Use Chinese labels for known action, status, priority, and trigger enums.
- Do not expose raw English enum labels in user-facing text.
- Use compact labels and values instead of long vertical definition lists.
- Preserve the disabled `重新分析 · 未启用` button.
- Keep the detail page read-only.

### Rationale Splitting

Add small frontend helpers that split long rationale text into display rows.

Preferred inputs, in order:

1. `trade_action.agent_reason`
2. `strategy.agent_reason`
3. `agent_report.summary_zh`
4. `trade_action.agent_excerpt`
5. `strategy.agent_excerpt`
6. `agent_report.raw_decision`

The helper should:

- Split on line breaks first.
- Then split long prose on Chinese and English sentence punctuation.
- Group adjacent sentences into short rows.
- Assign simple role labels by keyword where possible.
- Fall back to neutral labels such as `依据一`, `依据二`, `结论`.

Keyword labels can be simple and local to the UI:

- Trend or technical terms: `趋势派`
- Risk, stop, drawdown, decay, contango, or sizing terms: `风控派`
- Macro, event, policy, earnings, oil, rate, or geopolitical terms: `事件派`
- Conclusion, action, buy, trim, sell, hold, position, or allocation terms:
  `组合结论`

This is display formatting only. It must not modify CSV files or persisted
reports.

## Backend Design

No backend contract change is required for the first implementation.

The existing `load_dashboard_state()` already merges holdings with:

- `agent_report`
- `strategy`
- `premarket_action`
- `trade_action`

Only add backend fields if implementation discovers a specific current field is
unavailable in `/api/dashboard`. Any added field should come directly from the
existing CSV row, not from new inference.

## Empty And Error States

Missing values:

- Numeric unknown: `-`
- Missing action: `暂无触发中的交易动作`
- Missing strategy: `暂无交易策略`
- Missing TradingAgents report: `暂无 TradingAgents 报告`
- Missing quote: `缺行情`

Review states:

- If `status=review`, the card and detail page must show `需复核`.
- If `error` is present, show it as the review reason.
- Missing quote or invalid sizing fields must remain review-only. The UI must
  not make them look executable.

Long text:

- Collapse English source by default.
- Avoid any fixed-height text wall in the first viewport.
- Keep expanded raw text scrollable if it is still long after splitting.

## Testing Strategy

Add focused tests without calling real Futu OpenD, TradingAgents, or model APIs.

Frontend-oriented tests should cover:

- `renderTradeActions()` renders compact action cards with Chinese labels,
  price, quantity, notional, and short reason.
- Right rail counts distinguish ready, review, and watch rows.
- Clicking or selecting `查看完整策略` opens the matching symbol detail view.
- Symbol detail renders the decision band before long rationale content.
- Long rationale text is split into multiple display rows.
- English source is collapsed by default.
- Unknown numeric values render as `-`, not `0`.

Backend tests are only needed if a backend field is added. If no backend field
is added, reuse existing dashboard merge tests and focus coverage on frontend
render helpers.

Browser verification:

- Start the local dashboard.
- Open it with Playwright.
- Confirm the right rail action cards show the VIXY-style decision summary.
- Open a holding with a trade action.
- Confirm the detail view shows the decision band, impact metrics, and split
  rationale rows.
- Confirm mobile width stacks cards and text without overlap.

## Out Of Scope

- Changing the `trade_actions.csv` fieldnames.
- Changing action generation, sizing, or REVIEW gating.
- Translating or rewriting persisted TradingAgents output.
- Running TradingAgents from the dashboard.
- Adding order placement or broker trading actions.
- Sending Feishu or macOS notifications.
