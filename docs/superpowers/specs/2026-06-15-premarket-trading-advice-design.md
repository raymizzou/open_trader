# Premarket Trading Advice Design

## Goal

Build a daily premarket workflow that reads the latest portfolio, runs
TradingAgents for each AI-eligible US symbol, uses a separate model prompt to
judge whether the latest advice is materially important, and writes a concise
action report.

The workflow does not place trades and does not require manual confirmation.
It follows the latest generated advice by updating local advice state and
generating reports for the user to review before market open.

## Inputs

Primary input:

```text
data/latest/portfolio.csv
```

Only rows with:

```text
ai_eligible=true
```

are sent through the trading advice workflow. In the current portfolio pipeline
this means US common stocks and ETFs. Cash, HK assets, options, funds, and money
market funds are excluded from TradingAgents analysis.

Required runtime inputs:

- `--date`: trading analysis date, formatted `YYYY-MM-DD`.
- `--portfolio`: path to portfolio CSV, defaulting to `data/latest/portfolio.csv`.
- TradingAgents local project path, defaulting to `/Users/ray/projects/TradingAgents`.
- Model configuration and API keys through environment variables.

## Outputs

The workflow writes both durable machine-readable state and a human-readable
premarket report.

```text
data/
  runs/
    <YYYY-MM-DD>/
      trading_advice.csv
      change_classifications.csv
      premarket_actions.csv

  latest/
    trading_advice.csv
    premarket_actions.csv

reports/
  premarket/
    <YYYY-MM-DD>.md
```

Output meanings:

- `data/runs/<YYYY-MM-DD>/trading_advice.csv`: complete latest advice for every
  analyzed symbol in the run.
- `data/runs/<YYYY-MM-DD>/change_classifications.csv`: model classification of
  whether each latest advice item is materially important compared with the
  previous advice.
- `data/runs/<YYYY-MM-DD>/premarket_actions.csv`: only items that should appear
  in the morning action report.
- `data/latest/trading_advice.csv`: latest advice snapshot used as the previous
  advice on the next run.
- `data/latest/premarket_actions.csv`: latest action list for future watchlist
  and alert modules.
- `reports/premarket/<YYYY-MM-DD>.md`: the user-facing daily report.

## Data Flow

```text
portfolio.csv
-> filter ai_eligible=true rows
-> run TradingAgents per symbol
-> normalize latest trading advice
-> load previous advice snapshot
-> classify material change with model + prompt file
-> write full advice and change audit CSVs
-> update latest advice snapshot
-> write premarket actions CSV
-> write Markdown action report
```

Each symbol is analyzed independently. A failure on one symbol should not stop
the whole run if other symbols can still produce advice. The failed symbol is
recorded with error status and excluded from the action report unless the
failure itself needs attention.

## Module Boundaries

### TradingAgents Adapter

Purpose: integrate the external local TradingAgents project without spreading
its details through the rest of Open Trader.

Responsibilities:

- Add the configured TradingAgents project path to Python import resolution.
- Instantiate `TradingAgentsGraph` using TradingAgents' default configuration.
- Call `propagate(symbol, date)` for each eligible symbol.
- Capture the returned decision and relevant raw text/state.
- Normalize output into a stable internal advice record.

The adapter should treat TradingAgents as an external dependency. If its return
shape changes, only this adapter should need updates.

### Advice Store

Purpose: keep durable latest advice per symbol for tomorrow's comparison.

Responsibilities:

- Read `data/latest/trading_advice.csv` when it exists.
- Write each run's complete `trading_advice.csv`.
- Atomically update `data/latest/trading_advice.csv` after a successful run.
- Preserve fields needed by the change classifier.

The advice store is append-auditable through dated run folders, while
`data/latest/trading_advice.csv` is the working state for the next run.

### Change Classifier

Purpose: use a model to decide whether latest advice is materially important
compared with prior advice and current portfolio context.

The classifier uses a version-controlled prompt file:

```text
src/open_trader/advice/prompts/change_classifier.md
```

Inputs per symbol:

- Current portfolio row.
- Previous advice snapshot for that symbol, if any.
- Latest TradingAgents advice for that symbol.
- Existing risk flag and portfolio weight.
- Current run date.

Required structured output:

```json
{
  "include_in_report": true,
  "change_type": "new_signal",
  "severity": "medium",
  "suggested_action": "reduce",
  "summary": "One sentence for the report.",
  "rationale": "Short explanation of why this matters now.",
  "watch_trigger": "Optional price or condition to monitor."
}
```

Allowed `change_type` values:

- `new_signal`
- `action_changed`
- `risk_changed`
- `trigger_changed`
- `no_material_change`

Allowed `severity` values:

- `low`
- `medium`
- `high`

The classifier, not fixed Python rules, decides whether an item enters the
premarket report. Python validates the model output schema and records invalid
outputs as errors instead of silently accepting them.

### Premarket Report Writer

Purpose: produce the daily morning report and machine-readable action list.

Report rules:

- Include only rows with `include_in_report=true`.
- Do not list every holding.
- Sort by severity, then portfolio weight, then symbol.
- Include symbol, current weight, latest action, change type, summary,
  rationale, and watch trigger when present.
- If there are no material changes, write a short report stating that no action
  items were generated.

## CLI

Add a command:

```bash
.venv/bin/python -m open_trader run-premarket \
  --date 2026-06-16 \
  --portfolio data/latest/portfolio.csv
```

Optional arguments:

```text
--data-dir data
--reports-dir reports
--tradingagents-path /Users/ray/projects/TradingAgents
--symbols NVDA,QQQ,VIXY
--dry-run
```

Behavior:

- `--symbols` limits analysis to a comma-separated subset, useful for testing.
- `--dry-run` writes run outputs but does not update `data/latest/trading_advice.csv`.
- The command exits nonzero only when no report can be produced at all. Partial
  symbol failures are recorded in run outputs.

## CSV Schemas

### trading_advice.csv

```csv
run_date,symbol,market,asset_class,portfolio_weight_hkd,risk_flag,source,advice_action,advice_summary,raw_decision,status,error
```

`source` is `tradingagents`. `status` is `ok` or `error`.

### change_classifications.csv

```csv
run_date,symbol,include_in_report,change_type,severity,suggested_action,summary,rationale,watch_trigger,status,error
```

### premarket_actions.csv

```csv
run_date,symbol,market,portfolio_weight_hkd,severity,change_type,suggested_action,summary,rationale,watch_trigger
```

## Error Handling

- Missing portfolio file: fail before running any symbol.
- Empty eligible symbol set: write a Markdown report stating no eligible US
  stocks or ETFs were found; exit `0`.
- TradingAgents failure for a symbol: record `status=error` in
  `trading_advice.csv`, continue other symbols.
- Classifier invalid JSON or schema failure: record `status=error` in
  `change_classifications.csv`, exclude from `premarket_actions.csv`.
- File promotion failures should preserve previous `data/latest` files where
  possible, matching the monthly import pipeline's approach.

## Testing Strategy

Use focused tests with fake adapters and fake classifier clients.

Test coverage should include:

- Reads `portfolio.csv` and filters only `ai_eligible=true`.
- Runs one advice request per eligible symbol.
- Reads previous advice and passes it into the classifier.
- Writes complete run CSVs and latest CSVs.
- Writes Markdown report with only `include_in_report=true` items.
- Writes "no material changes" report when no items are included.
- Supports `--symbols` subset.
- Supports `--dry-run` without updating latest advice.
- Continues when one symbol fails.
- Rejects invalid classifier output schema.
- CLI wires paths, date, and options correctly.

No test should call the real TradingAgents API or external model by default.
Real TradingAgents smoke can be a manual command after the unit-tested pipeline
is implemented.

## Out Of Scope

- Automatic order placement.
- Human approve/reject workflow.
- Real-time price watching and alerts.
- HK-stock analysis.
- Broker API integration.
- Backtesting.

Those belong in later phases after daily premarket advice is reliable.
