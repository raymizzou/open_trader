# Open Trader Monthly Portfolio Design

## Goal

Build a monthly, repeatable portfolio aggregation and US-stock analysis workflow.
The user provides monthly PDF statements from Futu, Tiger, and Phillip. The
system extracts all assets, converts values to HKD using one external
month-end FX source, merges identical holdings across brokers, applies
position-risk flags, and produces one user-facing table: `portfolio.csv`.

Only US common stocks and ETFs are sent to TradingAgents for AI analysis.
No trade execution is automated.

## Inputs

Monthly PDFs are stored by month:

```text
data/
  statements/
    2026-05/
      futu.pdf
      tiger.pdf
      phillips.pdf
```

The initial supported brokers are:

- Futu
- Tiger
- Phillip

The source PDFs should remain local files and should not be committed to git.

## Persistent Output Layout

Each run writes both traceable intermediate files and the final user-facing CSV:

```text
data/
  runs/
    2026-05/
      manifest.csv
      extracted_positions.csv
      extracted_cash.csv
      portfolio.csv
      parse_warnings.csv

  latest/
    portfolio.csv
```

`data/latest/portfolio.csv` is always the latest result the user opens. The
intermediate files exist for debugging, auditability, and parser maintenance.

## Pipeline

```text
PDF statements
-> broker-specific parsers
-> normalized positions and cash
-> external month-end FX conversion to HKD
-> merge same market + asset_class + symbol + currency
-> calculate HKD portfolio weights
-> apply risk flags
-> write data/runs/YYYY-MM/portfolio.csv
-> update data/latest/portfolio.csv
```

The monthly import command should look like:

```bash
python -m open_trader import-statements --month 2026-06 \
  --futu data/statements/2026-06/futu.pdf \
  --tiger data/statements/2026-06/tiger.pdf \
  --phillips data/statements/2026-06/phillips.pdf
```

If a PDF is replaced for the same month, the command can be rerun. The manifest
uses file hashes to make replacements visible.

## Final Portfolio CSV

`portfolio.csv` is the only regular user-facing table. It has one row per
merged holding, not one row per broker account.

Columns:

```csv
sort_group,market,asset_class,symbol,name,currency,total_quantity,avg_cost_price,last_price,market_value,cost_value,unrealized_pnl,unrealized_pnl_pct,fx_source,fx_date,fx_to_hkd,market_value_hkd,cost_value_hkd,portfolio_weight_hkd,brokers,accounts,ai_eligible,analysis_symbol,risk_flag,confidence,notes
```

Field rules:

- `market`: `US`, `HK`, `OTHER`, or `CASH`.
- `asset_class`: `stock`, `etf`, `fund`, `money_market_fund`, `option`,
  `cash`, or `unknown`.
- `symbol`: broker/security symbol. Cash uses synthetic symbols such as
  `USD_CASH` and `HKD_CASH`.
- `total_quantity`: total quantity after merging broker holdings.
- `avg_cost_price`: weighted average cost price when cost data is available.
- `market_value`: original-currency market value.
- `fx_source`: external month-end FX provider identifier.
- `fx_date`: month-end FX date used for conversion.
- `fx_to_hkd`: original currency to HKD conversion rate.
- `market_value_hkd`: HKD market value.
- `cost_value_hkd`: HKD cost value when cost data is available.
- `portfolio_weight_hkd`: `market_value_hkd / total_portfolio_value_hkd`.
- `brokers`: semicolon-separated source brokers.
- `accounts`: semicolon-separated account aliases.
- `ai_eligible`: `true` only for US common stocks and ETFs.
- `analysis_symbol`: ticker passed to TradingAgents, such as `NVDA`.
- `risk_flag`: `normal`, `overweight`, or `data_check`.
- `confidence`: parser confidence, `high`, `medium`, or `low`.
- `notes`: human-readable parser or data notes.

Sorting:

1. US stocks and ETFs with `ai_eligible=true`
2. Other US assets
3. HK assets
4. Other non-cash assets
5. Cash

Rows within each group are sorted by `market_value_hkd` descending.

## Merge Rules

Holdings are merged by:

```text
market + asset_class + symbol + currency
```

Merge behavior:

- Quantities are summed.
- Market values are summed.
- Cost values are summed where available.
- Weighted average cost is computed from total cost and total quantity.
- Unrealized PnL is computed from market value and cost value when possible.
- If broker-provided unrealized PnL is more reliable, keep it as the primary
  value and use computed PnL as a validation check.
- If names differ across brokers, choose the longest or most common name.
- Source brokers and account aliases are retained in semicolon-separated fields.

## FX And Risk Basis

HKD is the portfolio risk basis currency.

All assets, including USD assets, HK assets, funds, options, and cash, are
converted to HKD using a single external month-end FX source. Broker-specific FX
rates are not used for portfolio risk weights.

Cash and money market funds are included in total portfolio value, but they are
not subject to overweight flags.

Risk rule:

```text
For non-cash, non-money-market-fund assets:
if market_value_hkd / total_portfolio_value_hkd > 10%
then risk_flag = overweight
```

`data_check` takes priority over `overweight` when required fields are missing
or parser confidence is low.

## PDF Parsing

Use broker-specific parser adapters:

```text
FutuStatementParser
TigerStatementParser
PhillipsStatementParser
```

Each parser extracts:

- statement period
- broker and account alias
- masked account identifier when available
- ending positions
- cash and unsettled cash balances
- currency
- market
- asset class
- quantity
- cost price or cost value
- last price
- market value
- unrealized PnL when available

Each parser writes normalized internal rows to:

- `extracted_positions.csv`
- `extracted_cash.csv`

Each parser writes parse issues to `parse_warnings.csv`.

`manifest.csv` records:

- month
- broker
- source file path
- source file hash
- parse timestamp
- page count
- parser version
- parse status

If a parser cannot confidently extract required fields, it must produce warnings
and mark affected rows as `data_check`. It must not silently produce final rows
that look reliable.

## Observed PDF Structure

Initial local inspection with `pdfplumber` showed:

- Tiger statement: 20 pages. Ending positions are around page 10, with columns
  for code, quantity, cost price, close price, market value, unrealized PnL,
  margin requirements, and currency.
- Phillip statement: 5 pages. Securities Portfolio is around page 2, with
  product code, display name, quantity, close price, market value, and margin
  fields.
- Futu statement: 9 pages. Ending stock and option overview is around page 6,
  with code/name, market, currency, quantity, price, market value, and margin
  fields.

## TradingAgents Integration

TradingAgents analyzes only rows matching:

```text
market = US
asset_class in stock, etf
ai_eligible = true
confidence != low
risk_flag != data_check
```

Analysis flow:

```text
portfolio.csv
-> filter eligible US holdings
-> sort by market_value_hkd descending
-> run TradingAgents per symbol
-> write analysis_results.csv
-> write watchlist.csv
```

`analysis_results.csv` columns:

```csv
month,analysis_date,symbol,name,portfolio_weight_hkd,current_rating,previous_rating,decision_summary,source_report_path,status,error
```

`watchlist.csv` columns:

```csv
symbol,name,current_price,portfolio_weight_hkd,target_action,trigger_type,trigger_value,trigger_note,priority,status
```

MVP reminder output is file-based only:

```text
data/latest/watchlist.csv
```

No automatic order placement is included.

## TradingAgents Runtime Findings

Local verification established:

- `TradingAgentsGraph` imports and initializes inside `open_trader/.venv`.
- `yfinance` can fetch NVDA historical data directly.
- OpenAI API connectivity works with the existing environment key.
- A clean-cache NVDA smoke run completed with final rating `Overweight`.

Important runtime requirements:

- Use a clean or managed TradingAgents cache. A prior empty cache file caused
  market data extraction to fail while the graph still produced a final rating.
- Add a data-quality gate before accepting analysis results.
- Configure longer LLM timeout and retries for TradingAgents calls. A default
  OpenAI call timed out during a risk analyst node.
- Store TradingAgents logs and cache under project-managed run directories, not
  global user home paths.

## Non-Goals For MVP

- No automated order placement.
- No real-time trading integration.
- No broker account API sync.
- No full transaction ledger reconstruction.
- No tax reporting.
- No OCR unless a supported PDF template later requires it.
- No in-app dashboard; CSV files are the first interface.

## Implementation Checkpoints

1. Create project package and CLI entrypoint.
2. Implement normalized data models.
3. Implement PDF parser adapters for Futu, Tiger, and Phillip.
4. Implement external month-end FX lookup and caching.
5. Implement merge, sorting, HKD weights, and risk flags.
6. Generate `portfolio.csv` plus intermediate audit files.
7. Add TradingAgents runner with timeout, retry, clean cache, and data gates.
8. Add tests with sanitized sample fixtures.
