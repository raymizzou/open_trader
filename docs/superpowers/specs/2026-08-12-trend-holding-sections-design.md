# Historical Trend Holding Sections Design

## Outcome

Split real broker positions into two vertically stacked sections:

1. `趋势持仓`
2. `非趋势持仓`

The same labels, order, and visual treatment apply to all three trend brokers and markets:

- Eastmoney / CN / A 股
- Phillips / HK / 港股
- Tiger / US / 美股

The split appears in both existing real-position surfaces:

- `持仓 → 真实持仓`
- `趋势报告 → 盘中持续 · 已有持仓 → 真实持仓`, including historical report views

`模拟盘持仓` is unchanged.

## Membership Rule

A current real position belongs to `趋势持仓` when either:

1. its normalized market and symbol appeared in a formal `BUY` action in any historical trend-report artifact for that broker; or
2. it appears in the source-controlled historical-evidence gap allowlist below.

Every other current real position belongs to `非趋势持仓`.

This rule is deliberately based on historical buy-plan membership, not on:

- whether the symbol appears in today's buy or hold plan;
- whether an attributed broker fill exists;
- how much of the current quantity came from the strategy;
- the current report's included/excluded row color.

Membership is permanent historical provenance. A symbol remains a trend symbol after it is sold, and is classified as trend again if it later reappears in the real account. If personal shares are added to the same broker position, the complete aggregated row remains in `趋势持仓`; quantities are never split.

### Historical-evidence gap allowlist

The allowlist is a narrow operator assertion for strategy positions whose original formal `BUY` artifact was not retained. It is keyed by broker and market so it cannot leak across accounts:

```text
Tiger / US: AMZN, CRNX, GRMN, KO, LH, NUE, REGN
```

These seven symbols are unioned with Tiger's historical formal-`BUY` membership after the same US symbol normalization. The allowlist is not inferred from current `HOLD` decisions, candidate rows, account holdings, or report presence, and it does not create a Dashboard editing UI or a general override system. Future additions require an explicit source change and regression-test update.

## Historical Projection

The Dashboard backend scans only the existing report directory selected by `TREND_REPORT_SOURCES`. It reuses the existing formal-action projection and symbol normalization, then unions the broker/market-specific source-controlled allowlist. For each broker it publishes one small read-only contract on the broker's trend-report payload:

```json
{
  "historical_buy_plan_membership": {
    "available": true,
    "symbols": ["US.ADP", "US.LPLA"],
    "reason": ""
  }
}
```

Keys use normalized `MARKET.SYMBOL` identity: uppercase US symbols, five-digit HK symbols, and six-digit CN symbols. Report revisions naturally collapse through set membership. The symbols array is sorted so the Dashboard payload is deterministic.

The backend adds the same contract to current, historical, and currently unavailable broker report projections. The Account snapshot contract remains unchanged; the browser joins the current Account positions to this read-only Dashboard contract by normalized market and symbol.

No database, new artifact, fill replay, cache, schema migration, or dependency is introduced.

## Failure Semantics

Classification must fail closed. A broker's membership contract is unavailable when:

- its report directory has no JSON history;
- a history artifact cannot be read or parsed as a JSON object;
- `strategy_judgments.formal_actions` is not a list;
- a formal `BUY` action does not have a valid symbol for that market.

The contract then contains `available: false`, an empty `symbols` array, and a short `reason`.

The allowlist does not turn an unreadable history into an available history. If historical scanning fails, the existing unavailable contract and single-table fallback remain in force so the UI never silently treats all non-allowlisted positions as non-trend.

When membership is unavailable, neither UI guesses that every row is non-trend. Both surfaces keep their existing single real-holdings table and show `历史买入计划归属暂不可用，未执行分组`. An available history with zero formal buys is distinct: both sections render normally and all current rows appear under `非趋势持仓`.

## UI Behavior

Both surfaces use the approved option A: two full-width sections stacked vertically. Each section reuses the surface's existing table renderer and preserves source order within the section.

The headings are exactly:

- `趋势持仓`
- `非趋势持仓`

The Account surface preserves all existing columns, live valuation, `做T` action, inline detail row, filters, totals, and account header. The two section row counts sum to the original row count, and their market values sum to the unchanged account holding value.

The Trend Report surface preserves all existing columns, current judgment, active protection line, row colors, source notice, and real/simulated tabs. Historical origin determines only which section contains the row; the existing current-report membership state continues to determine its row color. This means a historical trend position may still be currently excluded by the report without moving to `非趋势持仓`.

Empty sections remain visible and show the existing `无` empty state so the isolation is explicit. Responsive behavior remains the current table/card behavior; no new secondary tabs or side-by-side compressed tables are added.

## Verification

Focused backend tests prove:

- union across multiple historical artifacts and revisions;
- CN, HK, and US symbol normalization;
- `BUY` membership without requiring a fill;
- permanent membership after later non-buy reports;
- unavailable versus valid-empty history.
- Tiger/US allowlist membership is added without a formal `BUY` artifact;
- the allowlist does not affect another broker or market;
- an unavailable historical scan still uses the unavailable contract rather than a partial allowlist result.

Focused Dashboard JavaScript tests prove:

- Account and Trend Report real holdings use the same membership contract;
- both surfaces render `趋势持仓` before `非趋势持仓`;
- a mixed-origin symbol remains one complete row;
- input row count and market-value total are conserved;
- Trend Report row states and Account interactions remain intact;
- simulated holdings remain one unchanged table;
- unavailable membership retains the old single-table layout with a warning.

Final review readiness still requires `make acceptance` to return `PASS`. After that, the exact accepted Git SHA must be redeployed and verified by PID, working directory, SHA, fresh logs, and HTTP 200 before asking for user review.

## Explicit Non-Goals

- Splitting a broker row by strategy-owned versus personal quantity
- Reclassifying from the current day's plan
- Renaming `非趋势持仓` to `被趋势持仓`
- Changing account totals, statement reconciliation, trading behavior, or order ownership
- Changing simulated holdings or report history navigation
- Adding a Dashboard editor or general-purpose manual classification override
