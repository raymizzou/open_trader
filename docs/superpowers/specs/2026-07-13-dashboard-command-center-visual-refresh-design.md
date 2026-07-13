# Dashboard Command Center Visual Refresh Design

## Goal

Refresh the existing Open Trader Dashboard with the approved light Command
Center visual style while preserving every currently displayed data field,
status, control, empty state, and interaction.

This is a presentation-only change. It must not add, remove, rename, merge, or
reinterpret Dashboard data.

The approved mock is stored in the local visual-companion session at:

`.superpowers/brainstorm/90817-1783928572/content/command-center-data-faithful-v3.html`

## Approved Direction

Use the light Command Center style:

- cool neutral page background;
- white surfaces with subtle borders and restrained shadows;
- near-black primary asset card;
- blue as the single interaction accent;
- compact, tabular numeric typography;
- tighter information hierarchy without reducing readable touch targets;
- low motion, limited to 150–250 ms hover, focus, and state transitions;
- visible keyboard focus and WCAG AA text contrast.

Do not introduce the dark Terminal Pro style, the editorial Calm Ledger style,
new charts, a global portfolio conclusion, or action badges in the holdings
table.

## Data Fidelity Contract

The redesigned page must continue to display the same current information.

### Header

Preserve:

- `Open Trader` and `持仓实时看板`;
- `策略回测`;
- all market filters;
- all dynamically rendered broker filters;
- the current-view label;
- current-view total asset value;
- holding asset value and holding weight;
- cash-like asset value and holding count;
- every broker summary card and its existing values;
- quote status;
- `刷新账户与行情`;
- every source-status row;
- last successful refresh time.

The source-status content remains in the Header. No new global `今日结论` or
portfolio recommendation is added.

### Main Workspace

Preserve the Kelly Lab entry and all of its current status, count, and button
behavior.

Preserve the holdings table's existing ten columns in their current order:

1. `明细`
2. `市场`
3. `标的`
4. `数量`
5. `成本价`
6. `实时价`
7. `美元市值`
8. `港元市值`
9. `持仓占总资产的占比`
10. `盈亏`

Preserve cash detail, symbol detail, broker/account detail, trading-decision
tabs, research chat, and the standard backtest workspace without changing their
content contracts or behavior.

## Layout And Styling

Keep the existing three-region Header structure and restyle it as a cohesive
operational band:

1. Brand, actions, and filters.
2. Current-view asset summary and broker summaries.
3. Quote and source status.

On desktop, keep these regions in one row when space permits. The asset summary
gets the greatest width because it contains the densest numeric information.

Keep the Kelly Lab entry and holdings panel full width below the Header. Do not
add a sidebar beside the holdings table.

Use the existing table's horizontal overflow behavior on narrow screens rather
than dropping columns. Header regions stack into one column on small screens,
and filters remain reachable without shrinking touch targets below 44 px.

Use system fonts only. Do not add web-font requests, icon packages, component
libraries, chart libraries, build tooling, or runtime dependencies.

## Implementation Boundary

The expected implementation is limited to:

- `src/open_trader/dashboard_static/dashboard.css`
- focused static-asset assertions in `tests/test_dashboard_web.py` only when
  needed to protect the approved visual contract

The current HTML already exposes the required Header regions, Kelly Lab entry,
and holdings table. The current JavaScript already owns all data rendering and
interactions. Do not modify `dashboard.js`, API payloads, backend code, or data
models for this visual refresh.

If implementation reveals that the approved layout cannot be achieved with the
current DOM, stop and revise this design before changing HTML or JavaScript.

## States And Accessibility

Existing loading, unavailable, stale, failed, selected, expanded, and disabled
states keep their current semantics and copy. Styling must make each state
distinguishable without relying on color alone.

Requirements:

- normal text contrast of at least 4.5:1;
- visible keyboard focus rings;
- minimum 44 px interactive targets for primary controls;
- no hover-only information;
- no layout-shifting pressed states;
- `prefers-reduced-motion` disables nonessential transitions;
- numeric cells use tabular figures;
- mobile layout has no page-level horizontal overflow; only the existing table
  wrapper may scroll horizontally.

## Verification

Focused automated verification must confirm:

- all current Header element IDs and controls remain present;
- all ten holdings columns remain present and ordered unchanged;
- existing Dashboard JavaScript tests continue to pass;
- the change does not alter API or backend files.

Browser verification must cover desktop and mobile flows, including:

- market and broker filtering;
- refresh state and source-status rendering;
- Kelly Lab entry;
- holdings table horizontal scrolling on mobile;
- cash view;
- holding expansion and all detail tabs;
- standard backtest entry and return;
- research chat open and close;
- keyboard focus visibility.

Per the project Dashboard Definition of Done, run `make acceptance` as the final
verification gate. Only `PASS` means the visual refresh is complete. `FAIL` must
be fixed and rerun; `BLOCKED` must be reported as blocked.

## Out Of Scope

- New or removed Dashboard data.
- New portfolio conclusions or recommendations.
- New trade-action presentation.
- New holdings columns, charts, sorting, or filters.
- Backend, API, notification, watcher, or service changes.
- Dark mode.
- A new design-system framework or frontend dependency.
