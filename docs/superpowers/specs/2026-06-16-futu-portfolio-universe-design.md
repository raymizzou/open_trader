# Futu Portfolio Universe Design

## Goal

Build a portfolio-derived Futu quote universe that excludes positions which are
not suitable for OpenD market snapshots.

## Scope

The universe is used for quote checks and future portfolio-driven Futu
monitoring. It does not change `portfolio.csv` generation or asset allocation
reporting.

## Inclusion Rules

Rows from `data/latest/portfolio.csv` are candidates only when:

- `total_quantity` is non-zero.
- `market` is `US` or `HK`.
- `asset_class` is one of `stock`, `etf`, `fund`, or `option`.
- `symbol` is present.

## Exclusion Rules

Rows are excluded when:

- `asset_class` is `cash`.
- `asset_class` is `money_market_fund`.
- `market` is unsupported.
- `symbol` is blank.
- `total_quantity` is zero or not numeric.

`money_market_fund` is excluded because Futu OpenD stock snapshot calls returned
`未知股票` for the current HKD money market fund position during live
verification.

## Code Mapping

- US rows map to `US.<SYMBOL>`.
- HK numeric rows map to `HK.<5-digit symbol>`.
- HK non-numeric rows map to `HK.<SYMBOL>`.

## Testing

Unit tests cover inclusion, cash exclusion, money-market-fund exclusion, invalid
quantity exclusion, and HK code normalization.
