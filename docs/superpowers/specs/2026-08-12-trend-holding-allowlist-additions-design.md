# Trend Holding Evidence Allowlist Additions Design

## Outcome

Extend the existing source-controlled `TREND_HOLDING_EVIDENCE_ALLOWLIST` with three confirmed historical-evidence gaps:

```text
Tiger / US: add US.XLV, US.PYPL
Phillips / HK: add HK.06823
```

`HK.06823` is the canonical Dashboard identity for the Phillips position whose live API fields are `name: HKT-SS` and `futu_symbol: HK.06823`. Classification uses the normalized `futu_symbol`; the display name is evidence for the identity, not a second key.

These entries are additive. All existing allowlist entries remain unchanged.

## Scope

The additions affect only trend-versus-non-trend classification of current real positions on the two existing Dashboard surfaces:

- `持仓 -> 真实持仓` (Account)
- `趋势报告 -> 盘中持续 -> 已有持仓 -> 真实持仓` (Trend Report)

On those surfaces, the complete current broker row for each added symbol belongs to `趋势持仓`. The existing columns, values, row state, totals, source order, and interactions remain unchanged.

The additions do not:

- change trading, order creation, order ownership, or execution;
- alter current or historical trend-report artifacts or their contents;
- change simulated holdings;
- add a Dashboard editor, runtime override, database record, or new API contract;
- infer membership from the live API name, current holdings, or current report decisions.

## Membership and Failure Semantics

The implementation extends only the existing broker-and-market-scoped allowlist values:

- `("tiger", "US")` gains `US.XLV` and `US.PYPL`;
- `("phillips", "HK")` gains `HK.06823`.

The existing historical scan remains authoritative. The backend must first read and validate every historical report artifact, collect normalized formal-`BUY` symbols, and only then union the matching allowlist entry.

Existing fail-closed behavior is unchanged. Missing, unreadable, malformed, or invalid historical evidence produces `available: false` with an empty symbol list. The allowlist must not publish a partial membership result or make an unavailable scan available. Both real-holdings surfaces then retain the existing ungrouped fallback instead of guessing that rows are trend or non-trend.

## Verification and Release

Focused tests must prove:

- Tiger/US membership contains the existing entries plus `US.XLV` and `US.PYPL` after a successful historical scan;
- Phillips/HK membership contains `HK.06823` after a successful historical scan;
- `HK.06823` classifies the live Phillips row identified by `futu_symbol: HK.06823`, regardless of the display name `HKT-SS`;
- each addition remains isolated to its specified broker and market;
- Account and Trend Report real-holdings surfaces classify the added rows as `趋势持仓` without changing row data or totals;
- trading, simulated holdings, and historical report contents remain unchanged;
- an unavailable historical scan still returns the existing empty, unavailable contract without any allowlist entries.

After focused tests and direct workflow checks pass, `make acceptance` is the final Dashboard gate. Only `PASS` is review-ready. Redeploy the exact accepted Git SHA, then verify the new PID, working directory, Git SHA, fresh logs, and HTTP 200 from the review URL before asking the user to review the implementation.
