# Canonical Portfolio Deduplication Design

## Goal

Make `data/latest/portfolio.csv` and dated `data/runs/<date>/portfolio.csv` a
canonical current-holdings table with no duplicate portfolio positions.

The daily HK and US reports should read this canonical portfolio directly. They
should not need a separate cleanup step before sending holdings to
TradingAgents, trading-plan generation, or trade-action generation.

## Problem

The current broker sync path can write two rows for the same portfolio holding
when different brokers report the same `market + symbol + currency` with
different asset-class values.

One observed case:

- Tiger reports `HK.01688` as `stock`, quantity `2640`, market value
  `25634.40 HKD`.
- Futu reports `HK.01688` as `unknown`, quantity `0`, market value `0.00 HKD`.
- The portfolio builder groups by `market + asset_class + symbol + currency`,
  so those rows do not merge.
- Trade actions later load the portfolio by `market + symbol` and fail with
  `duplicate portfolio position(s): HK.01688`.

That failure is correct at the trade-action safety layer, but the duplicate
should never reach the canonical portfolio artifact.

## Decisions

- `portfolio.csv` represents portfolio-level current holdings, not broker-level
  detail rows.
- Broker-level positions remain available in `extracted_positions.csv`,
  `extracted_cash.csv`, snapshots, broker reports, and dashboard detail views.
- Canonical portfolio rows are unique by `market + symbol + currency`.
- Asset class is normalized before grouping so `unknown` does not split a row
  from a known class such as `stock`.
- Zero-quantity duplicate broker rows do not create separate canonical holdings
  when another broker reports a non-zero position for the same identity.
- If a conflict cannot be merged safely, broker sync writes dated diagnostics
  but does not promote `data/latest/portfolio.csv`.

## Canonical Identity

The canonical position identity is:

```text
market + symbol + currency
```

This intentionally excludes `asset_class` because asset class may be missing or
less precise from one live broker feed. Asset class remains a row attribute
derived from the grouped broker positions.

Cash rows keep the existing cash identity:

```text
CASH + currency
```

## Asset-Class Normalization

When multiple broker detail rows share a canonical identity, choose the canonical
asset class by priority:

```text
stock > etf > fund > option > money_market_fund > unknown
```

The exact priority should stay local to portfolio construction and be covered by
tests. Known asset classes override `unknown`; two distinct known classes for
the same identity are a data conflict.

Examples:

- `stock + unknown` becomes `stock`.
- `etf + unknown` becomes `etf`.
- `stock + etf` is a conflict and blocks latest promotion.

## Position Merge Rules

For mergeable rows with the same canonical identity:

- Sum `quantity`.
- Sum `market_value`, `cost_value`, `market_value_hkd`, and `cost_value_hkd`
  using the existing FX provider logic.
- Sum `unrealized_pnl` when all rows provide it; otherwise preserve the existing
  fallback behavior based on market value and cost value.
- Recompute `avg_cost_price`, `last_price`, `unrealized_pnl_pct`, and
  `portfolio_weight_hkd` from the merged values.
- Merge `brokers` and `accounts` as sorted semicolon-separated lists.
- Keep the longest non-empty display name.
- Merge notes for traceability.
- Use the lowest confidence among grouped rows.
- Mark `risk_flag=data_check` when confidence is low or required values are
  incomplete.

Zero-quantity rows are still included in broker detail artifacts. In the
canonical portfolio, a zero-quantity row should not create a second holding when
a non-zero row with the same canonical identity exists. If all rows for an
identity are zero quantity, the merged result may remain a zero canonical row
only when it is needed to reflect broker-reported residual value or data-check
state.

## Conflict Handling

Broker sync should detect conflicts before promoting latest artifacts.

Blocking conflicts:

- Same `market + symbol` appears with multiple non-cash currencies.
- Same canonical identity has conflicting known asset classes.
- Required numeric values are malformed in a way that prevents safe totals.
- Merged quantity, value, or cost data is internally inconsistent enough that
  trade-action sizing would be unsafe.

For blocking conflicts:

- Write dated snapshots, extracted detail files, merged attempt output when
  useful, and a broker sync report.
- Include the exact conflicted symbols and reasons in the report and CLI
  message.
- Do not update `data/latest/portfolio.csv`.
- Do not silently drop non-zero positions.

Non-blocking cleanup:

- Known asset class overriding `unknown`.
- Zero-quantity duplicate row suppressed into the canonical group.
- Missing optional display fields when numeric totals are safe.

## Data Flow

1. Futu and Tiger live sync fetch broker snapshots.
2. Each broker maps raw rows to broker detail `Position` and `CashBalance`
   objects.
3. Sync loads preserved broker detail rows for other brokers.
4. The portfolio builder normalizes positions into canonical groups by
   `market + symbol + currency`.
5. The builder writes a deduplicated dated `portfolio.csv`.
6. If no blocking conflict exists and `--update-latest` is set, sync promotes the
   deduplicated portfolio to `data/latest/portfolio.csv`.
7. Daily HK and US runs read `data/latest/portfolio.csv` without an extra
   deduplication step.

## Affected Components

- `src/open_trader/portfolio.py`
  - Update portfolio grouping and asset-class normalization.
  - Keep cash grouping behavior intact.
- `src/open_trader/futu_account.py`
  - Use the canonical portfolio builder when merging Futu live data with
    preserved detail rows.
  - Preserve existing mixed-row safety when no detail rows exist.
- `src/open_trader/tiger_account.py`
  - Use the same canonical portfolio builder when merging Tiger live data with
    preserved detail rows.
  - Preserve existing mixed-row safety when no detail rows exist.
- `src/open_trader/trade_actions.py`
  - Keep duplicate-position rejection as a defensive guard.
  - Do not rely on this layer for normal deduplication.
- Dashboard
  - Continue using broker detail files for broker-specific drilldown.
  - Use the canonical portfolio rows for top-level holdings.

## User-Facing Behavior

After the fix:

- `data/latest/portfolio.csv` contains at most one row per
  `market + symbol + currency`.
- Daily HK and US reports do not fail because of duplicate rows caused by
  `stock` versus `unknown` broker asset classes.
- The dashboard can still show per-broker detail for the merged holding.
- If a true conflict appears, the sync report explains it and latest promotion
  is blocked.

## Testing

Add focused tests for:

- `portfolio.py` merges `HK.01688 stock` from Tiger with `HK.01688 unknown`
  zero-quantity Futu into one canonical row.
- Known asset class overrides `unknown`.
- Conflicting known asset classes block latest promotion or raise a clear
  portfolio-build error.
- Same symbol across different currencies blocks latest promotion.
- Futu sync writes deduplicated dated and latest portfolio artifacts.
- Tiger sync writes deduplicated dated and latest portfolio artifacts.
- Trade-action duplicate guard remains in place for malformed external
  portfolios.
- Existing dashboard broker-detail behavior still receives separate broker
  detail rows.

## Out Of Scope

- Automatic order placement.
- Changing TradingAgents prompts.
- Removing broker detail artifacts.
- Redesigning dashboard layout.
- Adding a separate daily-only portfolio cleanup file.
