# Holdings Table Redesign Design

## Goal

Refactor the local dashboard holdings table so it is easier to scan as an asset
list. The current table gives too much horizontal space to the symbol column and
shows columns that are not useful in the main list. The redesigned table keeps
the existing dashboard shell and interaction model, but changes the holdings
table to a compact, market-sectioned layout.

## Selected Approach

Use the approved A layout: a single compact holdings table with one header and
market section rows.

The table is ordered by market section:

1. `US 美股持仓`
2. `HK 港股持仓`
3. `其他市场持仓`, only if non-US/HK non-cash holdings exist

Each market section has a full-width divider row before its holdings. The
divider row shows the market label, visible row count, HKD market value subtotal,
and subtotal weight against total assets. The divider uses a clear top border
and a distinct background so US and HK are visually separated without splitting
the page into tabs or repeated tables.

Cash-like rows continue to use the existing cash view behavior and are not part
of the normal holdings table.

## Main Table Columns

The main table columns are:

- `明细`: keep the existing `交易决策` button and row expansion behavior.
- `市场`: keep the market code.
- `标的`: keep symbol and name, but constrain the width so it no longer takes a
  third of the table. The symbol remains prominent; the name is secondary text.
- `数量`: keep total quantity.
- `成本价`: rename from the current `持仓价` label and render `avg_cost_price`.
- `实时价`: keep live quote price when available, with the existing fallback
  behavior for missing quotes.
- `美元市值`: show original `market_value` only when `currency=USD`; otherwise
  show `-`. This column does not convert HKD holdings into USD.
- `港元市值`: keep `market_value_hkd`.
- `持仓占总资产的占比`: render `portfolio_weight_hkd`.
- `盈亏`: render `unrealized_pnl_pct`.

The main table removes these columns:

- `券商`: move broker information into the expanded detail area.
- `动作`: remove from the main list. Existing trade-action information remains
  available in detail views where already supported.

## Detail Area

The `交易决策` expansion keeps its current role as the per-symbol detail surface.
Broker and account information should be visible there through the existing
broker detail section. If useful during implementation, the section label can be
kept as `券商账户明细`.

The broker detail table can continue to show broker, account, quantity, cost
price, statement/latest price, market value, and PnL. This design does not add
new broker backend behavior.

## Data Rules

The frontend should derive the redesigned columns from the existing dashboard
payload where possible:

- `成本价`: `holding.avg_cost_price`
- `实时价`: `quote.last_price` through existing quote rendering, falling back as
  the dashboard already does
- `美元市值`: `holding.market_value` only when `holding.currency === "USD"`
- `港元市值`: `holding.market_value_hkd`
- `持仓占总资产的占比`: `holding.portfolio_weight_hkd`
- `盈亏`: `holding.unrealized_pnl_pct`

Market section subtotals should be computed from the currently visible rows
after filters are applied. Subtotals use numeric `market_value_hkd` and
`portfolio_weight_hkd` values when parseable. If a subtotal cannot be computed
because the relevant values are missing or malformed, show `-` instead of
coercing the value to zero.

Filtering behavior stays aligned with the existing market and broker filters:

- `ALL` market view shows the market sections in fixed US, HK, other order.
- `US` market view shows only the US section.
- `HK` market view shows only the HK section.
- `CASH` market view keeps the existing cash detail panel.
- Broker filters apply before section rows are built.

## Visual Behavior

The table stays dense and utilitarian, matching the existing dashboard visual
system. Numeric columns are right-aligned with tabular numbers. The symbol
column has a stable constrained width instead of expanding to fill unused space.

Desktop behavior:

- One sticky header.
- One full-width section divider per visible market section.
- No repeated table headers between US and HK.

Small-screen behavior:

- Preserve the existing horizontal table scrolling pattern.
- Keep stable column widths so the table does not resize when quotes refresh.
- Section divider rows span all columns and remain readable inside the scroll
  area.

## Error Handling

Missing values display as `-`. The dashboard must not display missing numeric
values as `0`.

Live quote failures continue to use the existing quote status and quote fallback
behavior. The holdings table should still render static portfolio values when
live quotes are unavailable.

Malformed numeric fields in section subtotal calculations should only affect the
section subtotal display. They should not block row rendering.

## Implementation Surface

Expected files:

- `src/open_trader/dashboard_static/index.html`: update table headers and column
  count.
- `src/open_trader/dashboard_static/dashboard.js`: update row rendering, section
  row generation, USD market value formatting, and empty/error colspan values.
- `src/open_trader/dashboard_static/dashboard.css`: constrain symbol width,
  style market section rows, and keep numeric columns stable.
- Tests under `tests/`: add or update dashboard/static assertions for the new
  headers, removed columns, USD market value behavior, and US/HK section order.

No backend contract change is required unless tests reveal an existing payload
field is unavailable in the frontend.

## Out Of Scope

- Replacing the dashboard shell or creating a standalone trading interface.
- Adding tabs or separate pages for US and HK.
- Adding order execution or new trading actions.
- Changing portfolio CSV generation.
- Converting HKD holdings into USD market value.
- Removing the existing per-symbol detail or research workflows.

## Verification

Implementation should be verified with:

- Focused unit/static tests for table rendering helpers where available.
- Existing Python tests that cover dashboard payload and web serving.
- Browser verification against the local dashboard using the existing
  Playwright/local Chrome strategy for this repo.
- Desktop and mobile screenshots or smoke checks confirming that:
  - The main table shows the approved columns.
  - `券商` and `动作` are absent from the main table.
  - US appears before HK.
  - US/HK divider rows are visually clear.
  - USD holdings show USD market value, while HK holdings show `-`.
  - Text and numeric values do not overlap.
