# Symbol Detail Agent Report Design

## Goal

Add a read-only symbol detail view to the local portfolio dashboard. The detail
view shows each holding's latest TradingAgents report, the trading strategy
derived from that report, and the current trade action generated from the
strategy.

The first version is an inspection surface only. It does not run TradingAgents,
call a model, write artifacts, send notifications, or place orders.

## Selected User Experience

Use a focused detail panel inside the existing dashboard.

The holdings list remains the default view. Each holding exposes a detail entry
point. Selecting it switches the dashboard content area into a symbol detail
view for that holding. The detail view has a compact header with:

- Back to holdings list.
- Market and symbol, such as `US.VIXY`.
- Holding name.
- Live quote status.
- Current trade action status.
- A disabled `重新分析` button marked `未启用`.

Returning to the list preserves the current market filter, broker filter, and
client-side list state.

The detail body has four sections:

- `概览`: quantity, cost price, live price, HKD market value, unrealized PnL,
  portfolio weight, and data health.
- `TradingAgents 报告`: rating, report summary, key rationale, and raw source
  excerpt.
- `交易策略`: entry range, add price, stop loss, target prices, max weight,
  catalyst, time horizon, and plan text.
- `当前交易动作`: action, priority, trigger status, suggested quantity,
  suggested notional, limit price, stop price, execution status, and review
  reason.

The existing right rail `今日交易动作` remains part of the holdings list view.
When the focused detail panel is active, the dashboard can hide the right rail
to give the report enough reading space. Returning to the list restores the
current layout.

## Data Sources

The backend reads existing local artifacts only:

- `data/latest/portfolio.csv`
- `data/latest/trading_advice.csv`
- `data/latest/trading_plan.csv`
- `data/latest/premarket_actions.csv`
- `data/latest/trade_actions.csv`
- latest broker detail files already used by the dashboard

The detail view intentionally reads only `data/latest/*` for agent and strategy
data. It does not search historical `data/runs/*` directories, because mixing
historical analysis with the latest portfolio would make the UI ambiguous.

If a latest CSV is missing, unreadable, or lacks a row for a symbol, the
dashboard still renders the holding. The missing section shows an explicit
empty state instead of failing the whole dashboard.

## Backend Design

Keep the existing dashboard API shape and extend each holding in
`GET /api/dashboard`.

The backend merges rows by this key:

```text
(market.upper(), symbol.upper())
```

HK symbols keep their existing display form in the portfolio row. Futu display
symbols continue to be derived by the existing quote helper logic.

Each holding receives these new objects:

```json
{
  "agent_report": {
    "available": true,
    "run_date": "2026-06-18",
    "rating": "Underweight",
    "summary": "...",
    "raw_decision": "...",
    "source_status": "ok",
    "fallback_reason": "",
    "fallback_from_date": "",
    "error": ""
  },
  "strategy": {
    "available": true,
    "run_date": "2026-06-18",
    "rating": "Underweight",
    "entry_zone_low": "",
    "entry_zone_high": "",
    "add_price": "",
    "stop_loss": "18.5",
    "target_1": "",
    "target_2": "",
    "max_weight": "",
    "catalyst": "",
    "time_horizon": "...",
    "plan_text": "...",
    "agent_reason": "",
    "agent_excerpt": "",
    "source_status": "ok",
    "fallback_reason": "",
    "fallback_from_date": "",
    "status": "active",
    "error": ""
  },
  "premarket_action": {
    "available": true
  },
  "trade_action": {
    "available": true
  }
}
```

`premarket_action` and `trade_action` should include their existing CSV fields.
The frontend can render the fields it knows and ignore the rest.

The backend should normalize missing sections to:

```json
{
  "available": false,
  "error": ""
}
```

This gives the frontend one simple contract for empty states.

## Frontend Design

The frontend stays as static HTML, CSS, and JavaScript served by the local
Python dashboard server. Do not introduce React, Vite, a database, or a new
frontend build step.

The JavaScript state gets a selected holding key:

```js
selectedHoldingKey: ""
```

When empty, render the current holdings list. When set, render the focused
detail panel from the already loaded `/api/dashboard` payload and live quote
payload.

Rendering rules:

- Show unknown numeric values as `-`, never as `0`.
- Continue using Chinese labels for action enums, status enums, priorities, and
  trigger statuses.
- Show `source_status=fallback` as `使用历史报告回退`.
- Show `status=error` or `manual_review` with the error or review reason.
- Keep the `重新分析` button disabled and visibly marked `未启用`.
- Render long raw report text in a collapsed section labeled `查看原始报告`.
- On mobile widths, stack all detail sections in a single column.

Empty-state copy:

- Missing TradingAgents report: `暂无 TradingAgents 报告`
- Missing strategy: `暂无交易策略`
- Missing trade action: `暂无触发中的交易动作`
- Missing broker detail: `暂无券商账户明细`
- Missing live quote: `缺行情`

## Read-Only Boundaries

This feature must not:

- Trigger TradingAgents.
- Call DeepSeek or OpenAI-compatible model APIs.
- Write `data/latest`, `data/runs`, or `reports`.
- Send Feishu or macOS notifications.
- Place orders or unlock broker trading.

The disabled `重新分析` button is only a placeholder for future work. It should
not have a click handler that starts analysis.

## Error Handling

Dashboard loading should stay resilient:

- Missing latest advice, plan, premarket action, or trade action CSVs should not
  fail `/api/dashboard`.
- Malformed optional CSV rows should be skipped for detail matching where
  possible.
- Existing required portfolio loading behavior should not be weakened.
- Backend responses should preserve raw field values but the frontend should
  avoid exposing raw English enum labels when a known Chinese label exists.
- Missing numeric fields should remain unknown.

If the whole dashboard load fails for an existing reason, keep the current
dashboard-level failure state.

## Testing Strategy

Add focused backend and frontend tests without calling real TradingAgents, real
model APIs, or Futu OpenD.

Backend tests:

- `load_dashboard_state()` merges `trading_advice.csv`,
  `trading_plan.csv`, `premarket_actions.csv`, and `trade_actions.csv` into the
  matching holding.
- Missing optional latest CSVs produce `available=false` sections and do not
  fail dashboard loading.
- Symbol matching is case-insensitive and market-aware.
- A holding without agent artifacts still keeps portfolio, broker detail, and
  quoteable display data.

Frontend tests:

- Rendering a selected holding shows report, strategy, action, and overview
  sections.
- Returning from detail restores the holdings list and keeps filters.
- Missing report or strategy sections show Chinese empty states.
- Long raw report text is collapsed by default.
- Known action and trigger enums render as Chinese labels.

Browser verification:

- Start the local dashboard.
- Open it with Playwright.
- Select a holding that has TradingAgents data.
- Verify the detail view shows report, strategy, and trade action content.
- Verify the disabled `重新分析` entry is visible.
- Return to the list and confirm the prior filter state remains active.

## Out Of Scope

- Running per-symbol reanalysis from the dashboard.
- Historical report browsing.
- Comparing old and new TradingAgents reports in the UI.
- Editing trading plans in the dashboard.
- Order review or broker order submission.
- Push notifications from the detail view.
