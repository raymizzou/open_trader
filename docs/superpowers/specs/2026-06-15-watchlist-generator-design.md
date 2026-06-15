# Watchlist Generator Design

## Goal

Build the next step after the daily premarket report: convert
`data/latest/premarket_actions.csv` into a stable, machine-readable
`data/latest/watchlist.csv`.

The watchlist is the bridge between morning advice and future intraday
monitoring. It does not fetch live prices, send alerts, or place orders. Its
job is to make each action item explicit enough that a later watcher can decide
whether a condition is monitorable.

## Inputs

Primary input:

```text
data/latest/premarket_actions.csv
```

The input already contains only material action items selected by the premarket
change classifier. Each row includes:

```csv
run_date,symbol,market,portfolio_weight_hkd,severity,change_type,suggested_action,summary,rationale,watch_trigger
```

Required runtime inputs:

- `--actions`: path to action CSV, defaulting to `data/latest/premarket_actions.csv`.
- `--data-dir`: data root, defaulting to `data`.
- `--date`: optional run date override. When omitted, keep each action row's
  `run_date`.

## Outputs

The command writes a dated audit file and updates the latest watchlist:

```text
data/
  runs/
    <YYYY-MM-DD>/
      watchlist.csv

  latest/
    watchlist.csv
```

The user-facing file for the next phase is:

```text
data/latest/watchlist.csv
```

## Watchlist Schema

`watchlist.csv` columns:

```csv
run_date,symbol,market,suggested_action,severity,portfolio_weight_hkd,trigger_type,operator,trigger_price,trigger_text,status,error
```

Field meanings:

- `run_date`: source action date.
- `symbol`: broker/portfolio symbol.
- `market`: market from the action row.
- `suggested_action`: action phrase from the premarket report.
- `severity`: low, medium, or high.
- `portfolio_weight_hkd`: current portfolio weight from the action row.
- `trigger_type`: `price`, `open_price`, `manual_review`, or `none`.
- `operator`: comparison operator for price triggers: `<`, `<=`, `>`, `>=`, or
  empty when not monitorable.
- `trigger_price`: numeric trigger price as text, empty when not monitorable.
- `trigger_text`: original trigger text or generated note.
- `status`: `active`, `manual_review`, `no_trigger`, or `error`.
- `error`: parse or validation error text, empty on normal rows.

## Trigger Parsing

The MVP uses conservative deterministic parsing. It should only create an
automatic price trigger when the text clearly includes a comparison and a
number.

Examples that become monitorable:

```text
below 95
under 95.50
breaks below 95
above 110
over 110.25
open below 95
open above 110
<= 95
>= 110
```

Mapping rules:

- `below`, `under`, `breaks below`, `<`, `<=` become downside price triggers.
- `above`, `over`, `breaks above`, `>`, `>=` become upside price triggers.
- Text containing `open below` or `open above` becomes `trigger_type=open_price`.
- Other price comparisons become `trigger_type=price`.

Rows with a non-empty trigger that cannot be parsed become:

```text
trigger_type=manual_review
status=manual_review
trigger_text=<original text>
```

Rows with an empty trigger become:

```text
trigger_type=none
status=no_trigger
```

The parser must not infer a price from support/resistance language if no
number is present.

## CLI

Add a command:

```bash
.venv/bin/python -m open_trader build-watchlist
```

Optional arguments:

```text
--actions data/latest/premarket_actions.csv
--data-dir data
--date 2026-06-16
--dry-run
```

Behavior:

- Writes `data/runs/<date>/watchlist.csv` every run.
- Updates `data/latest/watchlist.csv` unless `--dry-run` is set.
- If `--date` is omitted, the run folder uses the newest `run_date` present in
  the actions file.
- If there are no actions, writes an empty watchlist with headers.
- If an action row is malformed, records an `error` watchlist row rather than
  failing the whole file when enough symbol context exists.

## Data Flow

```text
premarket_actions.csv
-> validate required action fields
-> parse watch_trigger conservatively
-> create watchlist rows
-> write dated watchlist.csv
-> optionally promote to data/latest/watchlist.csv
```

## Error Handling

- Missing input file: command exits nonzero with a clear error.
- Missing required CSV columns: command exits nonzero, because the whole input
  shape is invalid.
- Per-row trigger parsing failure: row becomes `manual_review`.
- Per-row malformed data: row becomes `error` when symbol and run date can still
  be identified.
- Latest promotion should use the same atomic copy/replace pattern as the
  premarket latest files.

## Testing

Add focused tests for:

- Watchlist row model and CSV field order.
- Parsing clear downside, upside, and open-price triggers.
- Non-parseable trigger text becoming `manual_review`.
- Empty trigger becoming `no_trigger`.
- Pipeline writing dated and latest watchlist files.
- `--dry-run` preserving existing latest watchlist.
- CLI wiring and argument validation.

## Non-Goals

- No live market data lookup.
- No price polling loop.
- No push notification, email, Telegram, or desktop alert.
- No broker API integration.
- No automatic order placement.
- No model-based trigger extraction in this phase.

Those belong after `watchlist.csv` is stable and manually reviewable.
