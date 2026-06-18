# Futu Account Sync Design

## Goal

Add a read-only Futu account sync path that pulls real-account holdings and
asset data from Futu OpenD, then merges those Futu rows into the existing
standard `portfolio.csv` contract.

The target workflow is mixed-source portfolio generation:

- Futu positions and cash come from live Futu OpenD account data.
- Non-Futu brokers continue to come from monthly statement imports.
- Downstream commands keep reading one standard portfolio CSV.

## Scope

This design covers two CLI entry points:

- `check-futu-account`: diagnose real Futu account access without writing files.
- `sync-futu-portfolio`: fetch real Futu account data, replace old Futu rows in
  the portfolio, and write a merged portfolio artifact.

The first version only uses `REAL` trading environment accounts. It does not
query paper accounts, does not unlock trading, does not store a trade password,
and does not place orders.

## Selected Approach

Use a focused Futu account import module plus a portfolio merger.

The module reads Futu account data through `OpenSecTradeContext`, maps returned
positions and cash into the repo's existing `Position` and `CashBalance` models,
and reuses `build_portfolio_rows()` to produce the same `portfolio.csv` schema
that the rest of the project already consumes.

Alternatives considered:

- Generate only a standalone `futu_portfolio.csv`. This is safer but keeps an
  extra manual merge step.
- Rebuild the entire import pipeline around multiple live and statement sources.
  This is closer to the long-term architecture but too broad for the first
  integration step.

## Architecture

Add a new module, `open_trader.futu_account`, with three responsibilities:

- `FutuAccountClient`: wraps `OpenSecTradeContext` and only calls read APIs such
  as `get_acc_list`, `accinfo_query`, and `position_list_query`.
- Mapping functions: convert Futu account, asset, cash, and position records
  into internal dataclasses and then into `Position` and `CashBalance`.
- Merge functions: load the existing portfolio CSV, keep non-Futu rows, replace
  Futu rows with live Futu rows, and write standard portfolio output.

Keep `open_trader.cli` thin. It should parse arguments, construct the client,
call module functions, print a concise summary, and close the Futu context.

## Data Flow

`check-futu-account`:

1. Connect to Futu OpenD with `OpenSecTradeContext`.
2. Query account list.
3. Filter to `REAL` securities accounts.
4. For each real account, query account funds and positions.
5. Print account count, position count, cash currencies, and diagnostic status.
6. Do not write files.

`sync-futu-portfolio`:

1. Read the existing portfolio CSV, defaulting to `data/latest/portfolio.csv`.
2. Keep rows whose `brokers` field does not include `futu`.
3. Connect to Futu OpenD and query `REAL` accounts.
4. Query account funds and positions for each real account.
5. Convert Futu holdings to `Position` rows and cash balances to `CashBalance`
   rows.
6. Combine live Futu rows with preserved non-Futu rows.
7. Recalculate portfolio totals, HKD values, weights, and risk flags through the
   existing portfolio builder.
8. Write dated artifacts.
9. Update `data/latest/portfolio.csv` only when explicitly requested and only
   when no blocking data errors occurred.

## Output Contract

The command writes these artifacts under `data/runs/<date>/`:

- `futu_account_snapshot.json`: diagnostic source snapshot from Futu after
  normalization to JSON-safe values.
- `portfolio.csv`: merged standard portfolio CSV.
- `futu_account_report.md`: Chinese user-facing report with account counts,
  holdings count, cash currencies, latest update status, and next actions.

When `--update-latest` is provided, the command also updates:

- `data/latest/portfolio.csv`

The latest update is skipped if the run has blocking Futu data errors.

## Portfolio Merge Rules

Futu live data replaces all existing portfolio rows whose `brokers` field
contains `futu`, case-insensitively.

Rows from other brokers are preserved as monthly-statement rows. This prevents
double counting old Futu statement rows while allowing Tiger, Phillips, and
other brokers to continue using the existing statement import path.

## Data Mapping

Futu position rows map to `Position`:

- `broker`: `futu`
- `account_alias`: stable account id or masked account label from Futu
- `market`: derived from Futu code prefix such as `US.` or `HK.`
- `asset_class`: stock or ETF when Futu returns enough type information;
  otherwise `unknown`
- `symbol`: stripped market code, preserving HK numeric symbols
- `currency`: returned currency, uppercased
- `quantity`: returned holding quantity
- `last_price`, `market_value`, `cost_value`, `unrealized_pnl`: parsed from Futu
  when present
- `confidence`: `high` when required numeric fields parse cleanly, otherwise
  `low`
- `notes`: source and parsing notes for review

Futu asset or cash records map to `CashBalance` by currency. Available balance
is preserved when Futu returns it. Missing optional fields stay blank rather
than being coerced to zero.

## Error Handling

Use structured error types that can be printed clearly by the CLI:

- `opend_unreachable`: TCP connection to OpenD failed.
- `trade_context_failed`: `OpenSecTradeContext` could not be created.
- `account_query_failed`: Futu account list query failed.
- `no_real_accounts`: no real securities accounts were found.
- `asset_query_failed`: funds query failed for an account.
- `position_query_failed`: position query failed for an account.
- `blocking_data_error`: Futu returned malformed required fields that make the
  merged portfolio unsafe to promote to latest.

An empty position list is not an error. It produces a portfolio with Futu cash
and preserved non-Futu broker rows.

Do not treat missing numeric fields as zero. Mark affected rows as low
confidence and prevent `--update-latest` when required sizing fields are
malformed or missing.

## CLI Behavior

`check-futu-account` options:

- `--host`, default `127.0.0.1`
- `--port`, default `11111`

`sync-futu-portfolio` options:

- `--portfolio`, default `data/latest/portfolio.csv`
- `--data-dir`, default `data`
- `--reports-dir`, default `reports`
- `--date`, optional run date in `YYYY-MM-DD`
- `--host`, default `127.0.0.1`
- `--port`, default `11111`
- `--update-latest`, default false

Successful output should print paths and counts:

- run date
- real account count
- Futu position count
- Futu cash currency count
- merged portfolio row count
- snapshot path
- portfolio path
- report path
- latest path when updated

## Testing

Automated tests use fake Futu clients by default. They should not require a real
Futu account or local OpenD process.

Coverage:

- Account diagnostics succeed for fake real accounts.
- Fake Futu holdings map to `Position` and cash maps to `CashBalance`.
- Old Futu rows are removed and non-Futu rows are preserved.
- Multiple real Futu accounts merge without double counting.
- Empty positions are accepted.
- Missing required numeric fields mark low confidence and block latest updates.
- OpenD unreachable, context failure, no real accounts, and query failures
  produce clear CLI errors.
- `check-futu-account` and `sync-futu-portfolio` CLI branches delegate business
  logic to `open_trader.futu_account`.

Manual verification after implementation should run against real OpenD:

```bash
.venv/bin/python -m open_trader check-futu-account
.venv/bin/python -m open_trader sync-futu-portfolio --date 2026-06-18
```

Only after reviewing the generated portfolio should a user run:

```bash
.venv/bin/python -m open_trader sync-futu-portfolio --date 2026-06-18 --update-latest
```
