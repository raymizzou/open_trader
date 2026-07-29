# Cross-Market Holding Order and Industry Display

## Problem

The shared Dashboard section `盘中持续 · 已有持仓` receives holding decisions
that already contain the frozen industry name, but the renderer does not show
that field. The projection also preserves account/report input order, so the
visible rows can appear alphabetically instead of following the approved
industry-first trend discipline. The defect affects CN, HK, and US reports.

## Scope

- Add `行业` to the shared sell/hold/review table contract for the holding
  section, with the same truthful missing-value behavior as other trend cells.
- In the Dashboard projection only, enrich missing holding display fields from
  `signal_snapshots.holdings` and sort `HOLD` rows using the frozen
  `industry_contexts` supplied by that report.
- Apply the same projection and renderer to CN, HK, and US.
- Use the existing discipline ordering: industry history change when complete,
  industry temperature, industry strength, warm-to-hot count, right-side
  share, then individual trend strength, right-side days, daily amount when
  available, and symbol.
- If a held row's required industry context is missing or invalid, fall back to
  the individual stock ordering for the holding list. Missing amount/days are
  omitted from the comparison; no value is invented.
- Regenerate CN/HK/US report artifacts through the existing revision/no-submit
  workflow after the code change. No order or ledger submission is part of this
  task.

## Non-goals

- Do not rewrite existing frozen report JSON or alter formal actions, strategy
  versions, risk rules, position sizing, or execution ledgers.
- Do not add a new browser-side sort implementation or a new dependency.
- Do not infer industry facts, turnover, or missing context values.

## Design

`dashboard._project_trend_actions` remains the single boundary for Dashboard
trend action projection. It copies each holding decision before enrichment,
fills only absent display/sort fields from the matching frozen holding signal,
and maps `industry_contexts` by industry ID/name. The resulting `hold_actions`
list is sorted before it is returned. The source payload is never mutated.

The existing market-neutral JavaScript renderer adds `行业` to the holding
table after `强度`. It reads the already projected `item.industry` and renders
`数据未提供` when absent. The same renderer remains selected for all three
markets, preserving the existing action, audit, and mobile table behavior.

## Failure behavior

The projection must remain usable for legacy reports with partial snapshots.
When a row cannot be tied to a valid industry context, the complete holding
list uses the deterministic individual fallback. When an individual sort field
is missing, that key is skipped and the later keys still provide a stable
order; symbol is the final tie-breaker.

## Verification

- Add a Python regression covering projection enrichment, industry-first order,
  invalid-context fallback, and source-payload immutability for all markets.
- Add a JavaScript renderer regression asserting the `行业` header/cell for CN,
  HK, and US.
- Run the affected Python/web suites and `git diff --check`.
- Run `make acceptance` only after source, tests, changelog, and regenerated
  report verification are final.
- After a passing acceptance, restart the exact accepted Dashboard SHA and
  verify PID, cwd, SHA, fresh log, HTTP 200, and the rendered three-market
  holding table.
