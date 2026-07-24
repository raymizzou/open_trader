# Trend Report UI Priority and Density Design

**Date:** 2026-07-24  
**Status:** Approved in conversation  
**Reference:** Approved visual-companion mockup `layout-v4`
**Markets:** CN, US, HK across every broker trend report

## Goal

Make the daily trend report read in decision order: show the summary and
today's sell/buy actions first, then holdings and industry context, then place
strategy evidence and operational diagnostics behind native disclosure
controls.

The implementation must preserve the current Dashboard visual language and
all report data. It changes presentation and hierarchy, not trading rules,
risk calculations, report generation, execution, or audit semantics.

The same hierarchy, density, semantic accents, list treatment, and disclosure
defaults apply to every CN, US, and HK trend report. Market-specific fields and
empty states remain market-specific, but no broker or market keeps the old
layout.

## Approved Page Order

The report renders in this order:

1. Compact report summary.
2. Blocking batch/revision notices, when present.
3. Sell actions, always visible.
4. Buy plan, always visible.
5. Manual-review actions, only when the report contains review items.
6. Current holdings, always visible.
7. Industry environment, always visible.
8. `纪律`, collapsed by default.
9. Portfolio plan risk, collapsed by default.
10. Strategy controller, collapsed by default.
11. Audit details, collapsed by default.

An empty manual-review section is omitted. Its count remains visible in the
report summary.

## Compact Report Summary

- Keep broker, market, report title, strategy version, and history control.
- Shorten static fact labels to `报告`, `数据`, `生成`, and `账户`.
- Render static facts as compact inline data bands rather than tall cards.
- Render count chips in action order: `卖出`, `买入`, `持有`, `复核`.
- Keep status and API cost in the same compact chip row.
- Static facts may be shorter than 44px. Buttons and disclosure summaries
  remain at least 44px high.

## Action Lists

Sell, buy, manual-review, and holding collections use one row per instrument.
Additional instruments append rows; they do not create more metric-card grids.
This row model applies to the CN-specific and market-neutral renderers used by
US and HK reports.

### Sell

The primary row shows:

- instrument;
- action;
- execution reference price;
- trigger reason; and
- active protection line.

Temperature transition, strength, and price-source details remain available as
secondary text inside the appropriate primary cells.

### Buy

The primary row shows:

- instrument;
- action;
- execution reference price;
- trend;
- industry;
- target weight;
- target amount;
- estimated quantity;
- estimated protection line; and
- planned risk.

The existing filter price, phase, strength, industry context, market cap,
turnover, risk reasoning, and execution facts remain in secondary cell text or
the existing subordinate detail rows. No report fact is discarded.

### Holdings and Manual Review

Holdings use a full-width list with instrument, current status, active
protection line, overheat tracking, and holding guidance. Manual-review rows
use the same full-width list treatment and render immediately after buy rows
when present.

## Industry Environment

- Render a full-width table, never a side-by-side peer of holdings or sell
  actions.
- Each industry is one row.
- Columns are industry, current temperature, temperature direction, strength,
  warm-to-hot count, and right-side share.
- Preserve invalid/missing context explanations without converting them to
  zero.

This removes the unequal-height two-column layout.

## Semantic Section Accents

Reuse the existing flat surfaces, borders, typography, and color tokens.
Differentiate only the left section rule:

- report summary: existing brand accent;
- sell: danger red;
- buy: success green;
- holdings: warning/amber;
- industry: muted information blue;
- folded evidence sections: subdued brand accent.

Text remains dark; color is supplementary, not the only status indicator.

## Discipline Disclosure

Rename `冻结策略纪律` / `策略参数快照` to `纪律`.

The outer `纪律` disclosure is collapsed by default on desktop and mobile. Its
summary shows:

`6 类 · N 项 · 本报告生成时参数`

When opened, render a responsive two-column list of six native nested
disclosures:

- 入场门槛
- 候选排序
- 仓位执行
- 持有管理
- 退出规则
- 其他设置

Each category summary shows its item count and a short fact preview. Opening a
category shows the report's complete frozen parameter rows. Remove repeated
phrases such as `冻结`, `影响 N 条纪律`, and repeated source-group prefixes from
visible labels.

## Other Disclosures

Portfolio plan risk, strategy controller, and audit details are native
`<details>` elements, collapsed by default.

Their summaries retain the most useful state:

- risk: budget state, used percentage, and remaining risk;
- controller: health, executor host, and latest success;
- audit: candidate, passed, and excluded counts.

The full existing content remains available after expansion.

## Density and Responsive Behavior

- Report and section gaps: 10px.
- Section padding: approximately 10px vertical and 12px horizontal.
- Static fact padding: approximately 5–6px vertical and 8–9px horizontal.
- Table-cell padding: approximately 6px vertical and 8px horizontal.
- Interactive buttons and disclosure summaries: minimum 44px height.
- Desktop action and context sections are full width.
- At 375px, tables become two-column fact cards with labels; the page must not
  create horizontal viewport scrolling.
- Focus rings remain visible and disclosure order matches visual/DOM order.

## Files and Scope

The smallest expected production change is:

- `src/open_trader/dashboard_static/dashboard.js`
- `src/open_trader/dashboard_static/dashboard.css`
- focused tests in `tests/test_dashboard_web.py`
- Playwright acceptance assertions in
  `src/open_trader/dashboard_acceptance.py`
- `CHANGELOG.md`

No new frontend framework, dependency, generic component system, or animation
is required.

## Verification and Acceptance

Development uses focused renderer tests and direct local Dashboard checks.
The final gate is `make acceptance`, which must exercise the real Dashboard
through Playwright.

Playwright acceptance must verify:

- desktop DOM and visual order for every available CN, US, and HK trend report;
- sell/buy/hold/industry sections are full width;
- buy and industry collections append as table rows;
- section left-border colors are semantically distinct;
- `纪律`, risk, controller, and audit are initially closed;
- disclosure summaries retain their compact facts;
- keyboard focus and native expand/collapse work;
- 375px rendering has no viewport-level horizontal overflow; and
- the accepted live page uses the expected worktree and Git SHA.

Acceptance must iterate all broker trend-report entry points exposed by the
live Dashboard and fail if any market still renders the retired layout or has
different disclosure defaults.

Only an acceptance `PASS` is review-ready. After `PASS`, redeploy the exact
accepted SHA and verify PID, working directory, Git SHA, fresh logs, and HTTP
200 before providing the review URL.
