# Tiger Account Sync Design

## Goal

Add a read-only Tiger Brokers account sync path that pulls real account holdings
and cash from Tiger OpenAPI, then merges Tiger rows into the existing standard
`portfolio.csv` contract.

The target workflow is mixed-source portfolio generation:

- Tiger positions and cash can come from live Tiger OpenAPI account data.
- Futu positions and cash can continue to come from Futu OpenD.
- Other brokers can continue to come from monthly statement imports.
- Downstream commands keep reading one standard portfolio CSV.

## Scope

This design covers two CLI entry points:

- `check-tiger-account`: diagnose Tiger OpenAPI configuration and read-only
  account access without writing files.
- `sync-tiger-portfolio`: fetch Tiger account data, replace old Tiger-only rows
  in the portfolio, and write merged portfolio artifacts.

The first version is read-only. It does not place orders, preview orders, modify
orders, subscribe to push events, or store credentials in the repo.

It supports stock and ETF positions first. Options, futures, funds, warrants,
and other derivatives are preserved in diagnostic snapshots but map to
`asset_class=unknown` or low-confidence rows until explicit downstream handling
exists.

## Selected Approach

Use a focused Tiger account import module plus a portfolio merger.

The module reads Tiger account data through the official `tigeropen` Python SDK,
maps returned positions and cash into the repo's existing `Position` and
`CashBalance` models, and reuses `build_portfolio_rows()` to produce the same
`portfolio.csv` schema that the rest of the project already consumes.

Alternatives considered:

- Generate only a standalone `tiger_portfolio.csv`. This is safer, but keeps a
  manual merge step and does not solve the daily workflow.
- Shell out to the `tigeropen` CLI and parse JSON output. This is useful for
  diagnosis but weaker as an application integration because it depends on CLI
  output stability.
- Rebuild a generic broker live-account framework before adding Tiger. This is
  cleaner long term but too broad for the first Tiger integration.

## Official API Basis

Tiger OpenAPI supports account and position reads through `TradeClient`:

- `get_managed_accounts()` returns available account profiles and account types.
- `get_positions()` returns current positions, including contract, quantity,
  average cost, market price, market value, and unrealized P&L.
- `get_assets()` is recommended for Global Account asset summaries.
- `get_prime_assets()` is recommended for Prime, standard, and paper account
  asset summaries.

The implementation should use these official docs as the source of truth:

- https://quant.itigerup.com/openapi/en/python/operation/trade/accountInfo.html
- https://quant.itigerup.com/openapi/en/python/quickStart/prepare.html
- https://quant.itigerup.com/openapi/en/python/permission/requestLimit.html
- https://github.com/tigerfintech/openapi-python-sdk

Account APIs are documented in the 60 requests per minute class, which is
enough for manual and daily portfolio sync. The sync should make one account
profile call, one positions call per account, and one asset call per account.

## Architecture

Add a new module, `open_trader.tiger_account`, with four responsibilities:

- `TigerAccountConfig`: loads SDK configuration from CLI options, environment,
  or the official properties path without logging secrets.
- `TigerAccountClient`: wraps `tigeropen.trade.trade_client.TradeClient` and
  only calls read APIs such as `get_managed_accounts`, `get_positions`,
  `get_assets`, and `get_prime_assets`.
- Mapping functions: convert Tiger account, asset, cash, and position objects
  into internal dataclasses and then into `Position` and `CashBalance`.
- Merge functions: load the existing portfolio CSV, keep non-Tiger rows, replace
  Tiger-only rows with live Tiger rows, and write standard portfolio output.

Keep `open_trader.cli` thin. It should parse arguments, construct the client,
call module functions, print a concise summary, and avoid interpreting SDK
objects directly.

## Configuration

Support the official `tigeropen` configuration file first, with environment
variables as an automation-friendly override.

Configuration inputs:

- `--config-dir`, default `~/.tigeropen/`
- `--account`, optional account id override
- `--sandbox`, default false
- `TIGEROPEN_TIGER_ID`
- `TIGEROPEN_ACCOUNT`
- `TIGEROPEN_PRIVATE_KEY_PATH`
- `TIGEROPEN_PRIVATE_KEY`
- `TIGEROPEN_SECRET_KEY`, optional pass-through for institutional accounts
- `TIGEROPEN_TOKEN`, optional pass-through when required by the account region

Prefer `TIGEROPEN_PRIVATE_KEY_PATH` or the properties file over raw multiline
private keys in environment variables. Never write private keys, tiger ids,
tokens, or full account ids to snapshots or reports.

## Data Flow

`check-tiger-account`:

1. Load Tiger SDK configuration.
2. Construct a read-only `TradeClient`.
3. Query managed accounts.
4. Select active accounts matching `--account` when provided, otherwise the
   configured default account.
5. Query positions for selected accounts with stock security type, all
   currencies, and all supported markets.
6. Query asset summary using account type:
   - Global account: `get_assets()`
   - Prime, standard, or paper account: `get_prime_assets()`
   - Unknown account type: try `get_prime_assets()` first and report the chosen
     method explicitly.
7. Print account count, account type, position count, cash currencies, selected
   asset method, and diagnostic status.
8. Do not write files.

`sync-tiger-portfolio`:

1. Read the existing portfolio CSV, defaulting to `data/latest/portfolio.csv`.
2. Stop if any row mixes `tiger` with another broker in the `brokers` field.
3. Preserve rows whose `brokers` field does not include `tiger`.
4. Load Tiger SDK configuration and query selected accounts.
5. Fetch positions and asset summaries for each selected account.
6. Convert Tiger holdings to `Position` rows and cash balances to `CashBalance`
   rows.
7. Combine live Tiger rows with preserved non-Tiger rows.
8. Recalculate portfolio totals, HKD values, weights, and risk flags through the
   existing portfolio builder.
9. Write dated artifacts.
10. Update `data/latest/portfolio.csv` only when explicitly requested and only
    when no blocking data errors occurred.

## Output Contract

The command writes these artifacts under `data/runs/<date>/`:

- `tiger_account_snapshot.json`: diagnostic source snapshot from Tiger after
  normalizing SDK objects to JSON-safe values and masking account identifiers.
- `portfolio.csv`: merged standard portfolio CSV.
- `tiger_account_report.md`: Chinese user-facing report with account count,
  account type, holdings count, cash currencies, latest update status, and next
  actions.

When `--update-latest` is provided, the command also updates:

- `data/latest/portfolio.csv`

The latest update is skipped if the run has blocking Tiger data errors.

## Portfolio Merge Rules

Tiger live data replaces existing portfolio rows only when the existing row is
Tiger-only:

- Replace rows where `brokers` is exactly `tiger`, case-insensitively after
  splitting on commas and semicolons.
- Preserve rows that do not include `tiger`.
- Stop with `mixed_tiger_broker_row` when a row mixes `tiger` with another
  broker, such as `futu;tiger`, `phillips;tiger`, or `futu;phillips;tiger`.

The current portfolio can contain mixed broker rows from monthly statement
aggregation. The first Tiger sync must not guess how much of those aggregate
rows belongs to Tiger. The user should regenerate or split those rows before
promotion.

## Data Mapping

Tiger position rows map to `Position`:

- `broker`: `tiger`
- `account_alias`: masked stable account alias such as `tiger_1234`
- `market`: derived from contract market, currency, or symbol conventions
- `asset_class`: stock or ETF when contract type confirms it; otherwise
  `unknown`
- `symbol`: contract symbol, uppercased
- `name`: contract name when returned, otherwise symbol
- `currency`: contract currency, uppercased
- `quantity`: `position_qty` when present, otherwise deprecated `quantity`
- `cost_price`: `average_cost` when present
- `last_price`: `market_price` when present
- `market_value`: `market_value` when present
- `cost_value`: `average_cost * position_qty` when both fields parse cleanly
- `unrealized_pnl`: `unrealized_pnl` when present
- `confidence`: `high` when identity and required numeric fields parse cleanly,
  otherwise `low`
- `notes`: source and parsing notes for review

Tiger asset rows map to `CashBalance` by currency:

- For Prime, standard, or paper accounts, read security segment currency assets
  from `get_prime_assets()`.
- For Global accounts, read currency market values or summary cash from
  `get_assets()`.
- Prefer per-currency cash balances over a single base-currency summary.
- Preserve available-for-trade or available-for-withdrawal when returned.
- Missing optional fields stay blank rather than being coerced to zero.

## Error Handling

Use structured error types that can be printed clearly by the CLI:

- `tigeropen_missing`: `tigeropen` is not installed.
- `config_missing`: required SDK configuration is missing.
- `config_invalid`: private key, account, or developer id configuration failed.
- `account_query_failed`: managed account query failed.
- `no_matching_accounts`: no active account matched the config or CLI filter.
- `asset_query_failed`: asset summary query failed for an account.
- `position_query_failed`: position query failed for an account.
- `mixed_tiger_broker_row`: existing portfolio row mixes Tiger with another
  broker and cannot be safely replaced.
- `blocking_data_error`: Tiger returned malformed required fields that make the
  merged portfolio unsafe to promote to latest.
- `rate_limited`: Tiger OpenAPI rejected the request due to rate limiting.

An empty position list is not an error. It produces a portfolio with Tiger cash
and preserved non-Tiger broker rows.

Do not treat missing numeric fields as zero. Mark affected rows as low
confidence and prevent `--update-latest` when required sizing fields are
malformed or missing.

## CLI Behavior

`check-tiger-account` options:

- `--config-dir`, default `~/.tigeropen/`
- `--account`, optional account id override
- `--sandbox`, default false

`sync-tiger-portfolio` options:

- `--portfolio`, default `data/latest/portfolio.csv`
- `--data-dir`, default `data`
- `--reports-dir`, default `reports`
- `--date`, required run date in `YYYY-MM-DD`
- `--config-dir`, default `~/.tigeropen/`
- `--account`, optional account id override
- `--sandbox`, default false
- `--update-latest`, default false

Successful output should print paths and counts:

- run date
- selected account count
- selected account type or asset method
- Tiger position count
- Tiger cash currency count
- merged portfolio row count
- snapshot path
- portfolio path
- report path
- latest path when updated

## Testing

Automated tests use fake Tiger clients by default. They should not require a
real Tiger account, OpenAPI credentials, or network access.

Coverage:

- Config loading succeeds from fake properties and environment inputs.
- Missing `tigeropen` and missing credentials produce clear CLI errors.
- Account diagnostics select the configured account and report the asset method.
- Fake Tiger positions map to `Position`.
- Fake Global Account assets map to `CashBalance` through `get_assets()`.
- Fake Prime Account assets map to `CashBalance` through `get_prime_assets()`.
- Tiger-only rows are removed and non-Tiger rows are preserved.
- Mixed Tiger broker rows block sync before promotion.
- Empty positions are accepted.
- Missing required numeric fields mark low confidence and block latest updates.
- Rate limit and SDK query failures produce structured CLI errors.
- `check-tiger-account` and `sync-tiger-portfolio` CLI branches delegate
  business logic to `open_trader.tiger_account`.

Manual verification after implementation should run against real Tiger OpenAPI
credentials:

```bash
.venv/bin/python -m open_trader check-tiger-account
.venv/bin/python -m open_trader sync-tiger-portfolio --date 2026-06-19
```

Only after reviewing the generated portfolio should a user run:

```bash
.venv/bin/python -m open_trader sync-tiger-portfolio --date 2026-06-19 --update-latest
```
