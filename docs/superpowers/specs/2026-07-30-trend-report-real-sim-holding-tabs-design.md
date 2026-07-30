# Trend Report Real and Simulated Holding Tabs

## Goal

Split the existing `盘中持续 · 已有持仓` section into two data views:

- `我的真实持仓`
- `我的模拟盘持仓`

The change applies identically to US, Hong Kong, and A-share trend reports.
The real-holding view is read-only and must never enter an automated order
path. The simulated-holding view keeps the current behavior.

## UI

Keep the existing section title, table, column order, cell formatting, desktop
layout, and mobile card layout unchanged. Add only a two-tab selector directly
under the existing section title.

- Default tab: `我的真实持仓`
- Second tab: `我的模拟盘持仓`
- Opening another market report resets the default to real holdings.
- The selection is local UI state; it is not persisted or added to the URL.
- No source, quantity, badge, or other new column is added.
- Every current column remains present and in the same order:
  `标的`, `动作`, `执行参考价`, `温度变化`, `节气`, `强度`, `行业`,
  `当前判断`, `活动保护线`, `持仓提示`.
- The existing table renderer and responsive card transformation are reused.
- An empty available view says `当前无真实持仓` or `当前无模拟盘持仓`.
- An unavailable real snapshot says `真实持仓数据不可用`.
- A legacy report without real-holding judgments says
  `当前报告未包含真实持仓判断`.

The real tab includes every current real-account position, including positions
outside the current simulated strategy report. Real positions with `卖出` or
`人工复核` judgments remain in this tab so the view never hides part of the
real account. The existing `动作` and `当前判断` columns carry those states.

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

Report generation performs these independent paths:

1. Build simulated strategy judgments and execution inputs exactly as today.
2. Load the configured real-account position snapshot for that market.
3. Evaluate every positive real position with the same market-specific trend
   signal pipeline.
4. Freeze the resulting read-only decisions in
   `real_holding_decisions`.

If the real snapshot cannot be loaded, the simulated report still completes.
It freezes an unavailable status and reason instead of treating the account as
empty.

The Dashboard continues to use the latest real-account snapshot as the source
of current real positions. It joins those positions to the frozen real
decisions by canonical market symbol:

- A matching decision supplies the existing trend-table fields.
- A current position added after the report remains visible as
  `人工复核 / 趋势数据不可用`.
- A position no longer present in the current real snapshot is not shown.

The Dashboard API projects a dedicated `real_position_actions` list for the
real tab. The existing `hold_actions` list remains the simulated tab source and
keeps its existing schema and meaning.

## Safety Boundary

`real_holding_decisions` is read-only evidence.

- No real order client consumes it.
- It cannot create or modify `formal_actions`.
- It cannot affect simulated execution, Kelly sizing, risk limits, report
  locks, action selection, or reconciliation. The report hash still freezes
  this new read-only evidence like every other report field.
- Missing real-account or signal data fails visibly, never by treating
  unavailable data as an empty account.
- Feishu trend-report behavior is unchanged.

## Compatibility

Existing frozen reports remain valid. The new fields are optional:

- Missing collection and status: show the legacy-report message in the real tab.
- Available collection plus zero current positions: show the empty state.
- Unavailable status or current real snapshot: show the unavailable state while
  keeping the simulated tab usable.

No historical report is rewritten.

## Verification

Focused automated checks must prove:

1. US, Hong Kong, and A-share report generation evaluates all positive real
   positions into the separate collection.
2. Real decisions cannot enter formal actions or any simulated/real execution
   client.
3. Missing per-symbol trend data retains the real row as `人工复核`.
4. Empty, unavailable, and legacy states remain distinct.
5. The Dashboard defaults to the real tab and switches to the simulated tab.
6. Both tabs render the exact existing columns, order, formatting, and mobile
   card behavior.
7. Available option anomaly data keeps the clickable button and dialog.
8. Missing option anomaly data renders no button.
9. Existing simulated holding projection and execution tests remain unchanged.

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
