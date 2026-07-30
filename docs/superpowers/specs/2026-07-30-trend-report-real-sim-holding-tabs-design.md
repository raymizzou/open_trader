# Trend Report Real and Simulated Holding Tabs

## Goal

Split the existing `盘中持续 · 已有持仓` section into two data views:

- `真实持仓`
- `模拟盘持仓`

The change applies identically to US, Hong Kong, and A-share trend reports.
The real-holding view is read-only and must never enter an automated order
path. The simulated-holding view keeps the current behavior.

## UI

Keep the existing section title, table, column order, cell formatting, desktop
layout, and mobile card layout unchanged. Add only a two-tab selector directly
under the existing section title.

- Default tab: `真实持仓`
- Second tab: `模拟盘持仓`
- Opening another market report resets the default to real holdings.
- The selection is local UI state; it is not persisted or added to the URL.
- No source, quantity, badge, or other new column is added.
- Every current column remains present and in the same order:
  `标的`, `动作`, `执行参考价`, `温度变化`, `节气`, `强度`, `行业`,
  `当前判断`, `活动保护线`, `持仓提示`.
- The existing table renderer and responsive card transformation are reused.
- Above the real table, reuse the existing broker, snapshot period, freshness,
  and read-only copy, for example `辉立 · 结单 2026-07-29 · 非实时 ·
  只读，不自动下单`.
- An empty available view says `当前无真实持仓` or `当前无模拟盘持仓`.
- An unavailable real snapshot says `真实持仓数据不可用`.
- A legacy report without real-holding judgments says
  `当前报告未包含真实持仓判断`.

The real tab includes every positive stock or ETF position frozen into that
report, including positions outside the simulated strategy report. Cash,
foreign exchange, money-market funds, options, and other unsupported assets
remain on the account holdings page. Real positions with `卖出` or `人工复核`
judgments remain in the real tab so the view never hides part of the frozen
real-account snapshot. The existing `动作` and `当前判断` columns carry those
states.

Real rows are ordered by urgency: `SELL_ALL`, `SELL_PARTIAL`, `MANUAL_REVIEW`,
then `HOLD`. Within each action group, reuse the existing trend holding order.

If the same symbol exists in both accounts, it appears once in each tab.

## Option Anomaly Button

Keep the current clickable button and dialog unchanged when
`option_anomaly.available === true`.

When option anomaly data is unavailable or missing, render the ordinary
identity cell and no button. Do not render a disabled placeholder. This applies
to US and Hong Kong buy and holding rows; A-share behavior remains unchanged.

## Data Model and Flow

The frozen trend report gains a separate, optional
`strategy_judgments.real_holding_decisions` collection. It uses the same row
shape and trend-signal rules as the existing holding rows, but it has no role in
formal actions, simulated position management, Kelly sizing, risk budgets,
execution batches, or order reconciliation.

Two sibling fields in `strategy_judgments` distinguish valid empty data from a
failed read:

- `real_holding_decisions_status`: `available` or `unavailable`
- `real_holding_decisions_reason`: present only when unavailable
- `real_holding_decisions_source`: broker, snapshot period, freshness text, and
  read-only notice

Report generation performs these independent paths:

1. Build simulated strategy judgments and execution inputs exactly as today.
2. Load the configured real-account position snapshot for that market.
3. Keep only positive stock and ETF positions.
4. Evaluate every retained real position with the same market-specific trend
   signal pipeline and a separate read-only real protection state.
5. Freeze the source metadata and resulting read-only decisions in
   `real_holding_decisions`.

If the real snapshot cannot be loaded, the simulated report still completes.
It freezes an unavailable status and reason instead of treating the account as
empty.

The real protection state follows the existing protection rules without
sharing state with the simulated strategy:

- A new position initializes its line from the latest valid cost/K-line inputs.
- A continuing position is recalculated for the next report, but its active
  line cannot fall below the prior real active line.
- Quantity or cost changes are treated as adds or reductions, not guessed as a
  new position lifecycle.
- Only a valid snapshot that omits a prior symbol closes that lifecycle. A later
  reappearance initializes a new line.
- An unavailable snapshot never clears protection state.
- Missing per-symbol signal or K-line data retains the row as
  `MANUAL_REVIEW`; it does not invent a protection line.

Each selected Dashboard report uses its own frozen real decisions and source
metadata. It never joins a historical report to the latest account snapshot.
Importing a newer statement does not revise or rewrite an existing report; the
next generated report reads it and recalculates the real decisions.

The Dashboard API projects the frozen collection as a dedicated
`real_position_actions` list for the real tab. The existing `hold_actions` list
remains the simulated tab source and keeps its existing schema and meaning.

## Safety Boundary

`real_holding_decisions` is read-only evidence.

- No real order client consumes it.
- It cannot create or modify `formal_actions`.
- It cannot affect simulated execution, Kelly sizing, risk limits, report
  locks, action selection, or reconciliation. The report hash still freezes
  this new read-only evidence like every other report field.
- Existing report counts, summaries, strategy decisions, Feishu notifications,
  and all execution paths continue to use simulated strategy data only.
- Missing real-account or signal data fails visibly, never by treating
  unavailable data as an empty account.
- Feishu trend-report behavior is unchanged.

## Compatibility

Existing frozen reports remain valid. The new fields are optional:

- Missing collection and status: show the legacy-report message in the real tab.
- Available collection plus zero frozen positions: show the empty state.
- Unavailable status: show the unavailable state while keeping the simulated
  tab usable.

No historical report is rewritten.

## Verification

Focused automated checks must prove:

1. US, Hong Kong, and A-share report generation evaluates all positive real
   stock/ETF positions into the separate collection.
2. Real decisions cannot enter formal actions or any simulated/real execution
   client.
3. Real protection state is separate, non-decreasing for a continuing position,
   reset only after a valid intervening snapshot proves the prior lifecycle
   closed, and preserved across unavailable snapshots.
4. Missing per-symbol trend data retains the real row as `人工复核`.
5. New statement imports affect only later reports; historical reports keep
   their frozen positions, source period, and decisions.
6. Empty, unavailable, and legacy states remain distinct.
7. The Dashboard defaults to the `真实持仓` tab and switches to `模拟盘持仓`.
8. Real rows use urgency-first ordering; simulated ordering remains unchanged.
9. Both tabs render the exact existing columns, order, formatting, and mobile
   card behavior.
10. Existing counts and Feishu output remain simulated-strategy-only.
11. Available option anomaly data keeps the clickable button and dialog.
12. Missing option anomaly data renders no button.
13. Existing simulated holding projection and execution tests remain unchanged.

After focused tests, run the real three-market workflow and the final
`make acceptance` gate. Only `PASS` is review-ready. Redeploy the exact accepted
SHA and verify Dashboard plus CN/HK/US PID, working directory, SHA, fresh logs,
heartbeat, blocker, and HTTP 200.

## Out of Scope

- Trading real accounts automatically
- Changing any existing table column or visual style
- Adding quantities or account-source columns
- Changing the order or design of other report sections
- Adding a new option-data request or fallback
- Changing Feishu notification content
