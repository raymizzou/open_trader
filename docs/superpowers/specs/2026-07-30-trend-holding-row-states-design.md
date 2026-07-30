# Trend Holding Strength Order and Row States

## Goal

Apply one shared rule to the `盘中持续 · 已有持仓` tables for A shares,
Hong Kong shares, and US shares, in both `真实持仓` and `模拟盘持仓`:

- sort rows by the report's numeric `strength`, descending;
- use a light green row background when the symbol appears in the current
  report's `可以买入` or `继续持有` collection;
- use a light pink row background when it appears in neither collection;
- use the existing soft gray background when the row is excluded by the trend
  lookup blacklist.

Missing strength sorts after numeric strength. A blacklisted row sorts last.
The existing deterministic individual holding key supplies tie-breakers.

## Membership

The Dashboard projection builds the included-symbol set from the already
projected `buy_actions` and `hold_actions`. It does not treat sell, review,
Top 10, scanned candidates, or account holdings alone as trend-report
membership.

Symbols are compared with the existing market-aware normalization so equivalent
CN, HK, and US symbol forms match. Each real and simulated row receives the same
projected membership state. A row whose existing reason is
`holding_trend_excluded` is blacklisted regardless of membership.

This is display metadata only. It does not alter frozen report JSON, strategy
judgments, formal actions, position sizing, execution, notifications, or
account data.

## UI

Keep the existing section, tabs, ten columns, column order, values, typography,
spacing, option-anomaly behavior, desktop table, and mobile card layout.

Only the row background changes:

- trend-report row: existing light green `#e7f4ec`;
- non-trend-report row: existing light pink `#fae8e6`;
- blacklisted row: existing `var(--surface-soft)`.

No label, badge, legend, new column, or font-color change is added.

## Compatibility and Failure Behavior

Legacy reports use the same projection from their available buy/hold
collections. Missing or invalid symbols do not gain membership by guesswork and
therefore use the non-trend background unless already blacklisted. Missing
strength remains visible and sorts last; no value is invented.

## Verification

- Add a Python regression proving shared membership classification and
  strength-first ordering, including missing strength and blacklist-last.
- Add a renderer regression proving all three row classes in CN, HK, and US,
  for both real and simulated tabs, without changing the ten-column contract.
- Run the focused Dashboard projection and browser-renderer tests plus
  `git diff --check`.
- After implementation and changelog are final, run `make acceptance`.
- Only after `PASS`, redeploy the exact accepted SHA and verify PID, cwd, SHA,
  fresh logs, and HTTP 200 before review.
