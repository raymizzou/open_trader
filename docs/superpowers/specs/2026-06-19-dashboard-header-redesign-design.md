# Dashboard Header Redesign Design

## Goal

Redesign the realtime portfolio dashboard header so the first viewport answers
four questions without the current left filter rail and low-value summary cards:

- What assets are in the current view?
- How are assets and cash split by broker?
- Which broker/data sources are live versus statement based?
- What filter is currently applied?

The approved mock is stored at
`.superpowers/brainstorm/3244-1781865990/content/header-dashboard-mock-v1.html`.

## Decisions

- The header summary is a current-view summary. Market filters and broker
  filters both change the header asset numbers.
- The left filter panel is removed. Market and broker filters move into the
  header as compact segmented controls.
- Broker assets are shown separately for `futu`, `tiger`, and `phillips`.
- Connection/source status is shown per broker:
  - Futu: quote connection plus live account sync state.
  - Tiger: live account sync state; quotes still come from the Futu quote path.
  - Phillips: monthly statement state, explicitly marked non-realtime.
- The existing `数据健康` card is removed from the first viewport.
- The low-weight `实时刷新状态`, `券商账户`, and `数据健康` cards are replaced by
  denser header content.

## Header Layout

The header becomes a single operational band with three regions:

1. Brand and filters
   - `Open Trader` and `持仓实时看板`.
   - Market segmented control: `全部市场`, `US`, `HK`, `现金`.
   - Broker segmented control: `全部券商`, `futu`, `tiger`, `phillips`.
   - A short current-view line such as `当前视图：US · futu · 8 个持仓`.

2. Current-view assets
   - Primary number: current filtered total asset value in HKD.
   - Secondary numbers: holding asset value, cash-like asset value, holding
     weight, cash weight, and holding count for the filtered view.
   - Broker mini cards showing each broker's asset, holding, and cash values.
   - The selected broker card is visually emphasized.

3. Broker source and refresh state
   - Global quote pill, refresh button, and last successful refresh timestamp.
   - Per-broker rows for Futu, Tiger, and Phillips.
   - Status copy must be Chinese and must distinguish live account data from
     monthly statement data.

## Data Model

Add dashboard summary data that can support filtered summaries without relying
on client-side guesswork over merged rows.

Recommended shape:

```json
{
  "summary": { "...": "existing all-portfolio summary" },
  "broker_summaries": [
    {
      "broker": "futu",
      "label": "富途",
      "portfolio_value_hkd": "970395.19",
      "holding_value_hkd": "230654.68",
      "cash_like_value_hkd": "739740.51",
      "holding_count": 8,
      "source_kind": "live_account"
    }
  ],
  "source_statuses": [
    {
      "broker": "phillips",
      "label": "辉立",
      "capability": "statement",
      "status": "non_realtime",
      "display_text": "2026-06-19 月结单导入"
    }
  ]
}
```

Implementation should avoid attributing full merged positions to every broker.
Broker-level asset and cash values should come from broker detail rows when
available, with `portfolio.csv` as the fallback only when detail rows are
missing. Missing or ambiguous values render as `-`, not zero.

## Filtering Behavior

The existing filter state remains conceptually the same:

- `marketFilter`: `ALL`, `US`, `HK`, or `CASH`.
- `brokerFilter`: `ALL` or one broker id.

When either filter changes:

- Holdings table updates to the filtered holdings.
- Visible count updates.
- Header current-view asset values update to the filtered rows.
- Broker mini cards remain visible so the user can switch quickly.

If `现金` is selected, the main content shows a compact cash detail view built
from cash-like `portfolio.csv` rows and broker cash detail rows when available.
The header values and visible count must match that cash view.

## Connection And Source Status

The connection area should not imply every broker has the same realtime
capability.

- Futu row:
  - Show quote status from `/api/quotes`.
  - Show account source as live when Futu live account data is present.
  - If quotes fail or become stale, show the existing diagnostic next step in
    Chinese.

- Tiger row:
  - Show account source as live when Tiger rows are present from live sync.
  - State that quote data uses the Futu quote path.
  - If Tiger account data is absent, show `未检测到账户同步`.

- Phillips row:
  - Show latest statement import date/month.
  - Always mark as `非实时` unless a future live API is added.

## Frontend Components

Keep this implementation in the existing static dashboard structure:

- Update `index.html` to replace the summary grid and filter rail with the new
  header band.
- Update `dashboard.css` with compact header layout, responsive wrapping, broker
  mini cards, and connection rows.
- Update `dashboard.js` to:
  - bind the moved filter controls,
  - compute or consume current-view summaries,
  - render broker mini cards,
  - render per-broker source rows,
  - keep quote refresh behavior unchanged.

The right rail `今日交易动作` remains in the main workspace. The holdings table
and symbol detail panel keep their current behavior.

## Error Handling

- If portfolio data fails to load, keep the existing dashboard failure state and
  show the header asset fields as `-`.
- If broker summaries are unavailable, render broker cards with `-` values and a
  source note such as `暂无明细`.
- If quote refresh fails, keep stale quote behavior and show the diagnostic in
  the Futu connection row.
- Do not display raw English enum labels in user-facing status text.

## Testing

Add focused tests for:

- Backend broker summaries split assets and cash by broker without double
  counting merged holdings.
- Missing broker detail rows fall back conservatively and render unknown values
  as blank/`-`.
- Dashboard HTML no longer includes the old left filter rail or `数据健康` card.
- Frontend filtering changes the header current-view summary.
- Quote failure still updates the global quote pill and the Futu connection row.

Verify manually with Playwright at desktop and mobile widths:

- Header text does not overlap.
- Filter controls remain reachable.
- Broker status rows are readable.
- Holdings table and `今日交易动作` remain visible in the first working viewport
  on desktop.
