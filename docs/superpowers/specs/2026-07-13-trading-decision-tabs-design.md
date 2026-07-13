# Trading Decision Tabs Design

## Goal

Replace the crowded trading-decision plugin grid with a conclusion-first tab
view. The tab panel keeps the current plugin section width and shows one module
at a time so cards no longer compete for space.

This is a frontend-only presentation change. It does not change dashboard data,
backend APIs, trading decisions, notifications, or order behavior.

## Selected Layout

Use a standard horizontal tab bar inside the existing `插件模块` section.

The tabs have a fixed order:

1. `最终决策`
2. `趋势 / K 线`
3. `新闻 / 舆论`
4. `富途异动`

Only the selected panel occupies layout space. The section keeps its existing
width and its height follows the selected content; it does not become a modal,
full-screen view, or fixed-height viewport.

On narrow screens, the tab bar stays on one line and scrolls horizontally. It
does not wrap into multiple rows. Use native buttons with tab semantics and
visible focus styles; do not add a tab library.

## Interaction

`最终决策` is selected whenever a holding's trading-decision detail is opened.
Switching to another holding also resets the selection to `最终决策` rather than
remembering the previously selected evidence tab.

Selecting a tab changes frontend state and replaces the visible panel without
fetching dashboard data again. All four tabs remain selectable even when their
module data is unavailable.

## Content Mapping

### Final Decision

Combine the existing large-model decision template and TradingAgents summary in
the first panel. Preserve the current conclusion-first content: comprehensive
conclusion, current action, core reason, and review or execution conditions.

### Trend / K-line

Reuse the existing K-line decision-facts content, including technical facts,
Bollinger-band presentation, and source details.

### News / Public Opinion

Reuse the existing news-sentiment content, including domestic discussion,
keywords, and source details.

### Futu Anomalies

Reuse the existing Futu anomaly-signal content.

The old placeholder-only modules are removed from this surface: company action,
fundamentals, earnings, broad market / industry, and portfolio risk. They can be
added back as real tabs only when they have a real dashboard data contract.

## Missing And Failed Data

The four expected tabs are always shown. A module without real usable data is
treated as unavailable rather than hidden:

- Render its tab in the existing failure/red visual language.
- Keep the tab clickable.
- In its panel, show the existing module error when one is available.
- Otherwise show `数据未生成`.

Do not add a second status model, retry control, aggregate empty state, or new
backend field for this redesign.

## Implementation Boundary

Keep the existing static frontend. The expected product changes are limited to:

- `src/open_trader/dashboard_static/dashboard.js`
- `src/open_trader/dashboard_static/dashboard.css`
- `tests/test_dashboard_web.py` for focused static frontend assertions

Add one small selected-tab state value and reuse the existing module renderers
and availability signals. Do not add dependencies, a component framework, a
build step, or a backend API change.

## Verification

Focused automated checks must cover:

- The fixed conclusion-first tab order.
- `最终决策` selected when detail opens.
- Selecting a tab replaces the visible panel.
- Unavailable modules remain visible, are marked red, and show an error or
  `数据未生成`.
- Opening a different holding resets the selected tab to `最终决策`.
- The narrow-screen tab bar remains a single horizontally scrollable row.

After implementation, run the relevant Dashboard tests and directly exercise
the browser workflow. As required by the project instructions, run
`make acceptance` last. Only its `PASS` result permits the behavior change to be
reported as complete.
