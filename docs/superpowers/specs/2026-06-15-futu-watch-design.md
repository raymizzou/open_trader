# Futu Watch Design

## Goal

Build the first live market-data loop for Open Trader by connecting to Futu
OpenD, reading the current watchlist, fetching US stock quotes, and recording
local alerts when a watch trigger is hit.

The first version is intentionally read-only. It proves that Open Trader can
connect to Futu's market data flow and retrieve quotes. It does not place
orders, unlock trading, sync broker holdings, send external notifications, or
monitor HK stocks.

## Runtime Assumptions

Futu OpenAPI uses a local or remote OpenD gateway. The command connects to
OpenD through the Python SDK, normally at:

```text
host=127.0.0.1
port=11111
```

The user must have OpenD running and logged in before starting the watcher. If
OpenD is not reachable, the command exits with a clear diagnostic.

The Python dependency is `futu-api`. If it is not installed, the command exits
with an install hint instead of a traceback.

## Inputs

Primary input:

```text
data/latest/watchlist.csv
```

The command also accepts an explicit path:

```bash
.venv/bin/python -m open_trader watch-futu \
  --watchlist data/runs/2026-06-15/watchlist.csv
```

This is useful while `data/latest/watchlist.csv` does not exist but a dated
watchlist already does.

Only rows matching all of these conditions are monitored:

- `market=US`
- `status=active`
- `trigger_type=price` or `trigger_type=open_price`
- `operator` is `<=` or `>=`
- `trigger_price` is a valid decimal value

The first version maps internal US symbols directly to Futu symbols:

```text
VIXY -> US.VIXY
QQQ -> US.QQQ
```

Rows that do not match the supported shape are ignored, not treated as fatal
errors.

## CLI

Add a long-running command:

```bash
.venv/bin/python -m open_trader watch-futu \
  --watchlist data/latest/watchlist.csv \
  --data-dir data \
  --date 2026-06-15 \
  --host 127.0.0.1 \
  --port 11111 \
  --poll-seconds 5
```

Arguments:

- `--watchlist`: watchlist CSV path, defaulting to
  `data/latest/watchlist.csv`.
- `--data-dir`: output data root, defaulting to `data`.
- `--date`: alert run date. When omitted, use the newest `run_date` in the
  loaded watchlist rows.
- `--host`: Futu OpenD host, defaulting to `127.0.0.1`.
- `--port`: Futu OpenD port, defaulting to `11111`.
- `--poll-seconds`: quote polling interval, defaulting to `5`.
- `--once`: optional diagnostic mode that performs one quote fetch, prints the
  result, then exits.

`--once` is part of the first version because it directly supports the core
goal: confirming that Futu quotes can be retrieved without leaving a watcher
running.

## Outputs

Alerts are appended to:

```text
data/runs/<YYYY-MM-DD>/alerts.csv
```

Fields:

```csv
alerted_at,run_date,symbol,market,futu_symbol,trigger_type,operator,trigger_price,last_price,suggested_action,severity,trigger_text
```

The command also prints progress and quote diagnostics to the terminal. Startup
output should include:

```text
connected to Futu OpenD at 127.0.0.1:11111
loaded N active US trigger(s)
quote US.VIXY last_price=...
```

If a trigger is hit, the terminal alert includes the symbol, latest price,
trigger condition, severity, and suggested action.

## Data Flow

```text
watchlist.csv
-> validate required columns
-> filter monitorable US trigger rows
-> convert symbols to Futu code format
-> connect to Futu OpenD
-> fetch quote snapshots every poll interval
-> evaluate trigger conditions
-> print alert
-> append alerts.csv
```

The first implementation uses polling. It should define the quote access behind
a small boundary so a future Futu subscription/callback implementation can
replace the quote source without changing trigger evaluation or alert writing.

## Module Boundaries

### Futu Quote Client

Purpose: isolate Futu SDK and OpenD behavior.

Responsibilities:

- Import and create `OpenQuoteContext`.
- Connect to the configured host and port.
- Fetch snapshots for a list of Futu symbols.
- Return normalized quote records with symbol and last price.
- Close the quote context on exit.
- Convert SDK errors into clear application exceptions.

The rest of Open Trader should not import `futu` directly.

### Watchlist Loader

Purpose: turn `watchlist.csv` into monitorable trigger objects.

Responsibilities:

- Validate required CSV columns.
- Filter unsupported rows.
- Validate trigger prices.
- Convert US symbols to Futu symbols.
- Pick the effective run date when `--date` is omitted.

### Watch Loop

Purpose: coordinate quotes, trigger evaluation, and alert output.

Responsibilities:

- Run startup diagnostics.
- Poll quotes until interrupted or `--once` completes.
- Evaluate `last_price <= trigger_price` and `last_price >= trigger_price`.
- Alert only once per symbol and trigger per process.
- Continue monitoring other symbols if one quote is missing or invalid.
- Close the Futu client on Ctrl-C.

### Alert Writer

Purpose: append durable local alert records.

Responsibilities:

- Create `data/runs/<date>/alerts.csv` when needed.
- Write the header on first creation.
- Append each triggered alert with stable field order.

## Error Handling

- Missing `futu-api`: exit nonzero with an install hint.
- OpenD connection failure: exit nonzero with host, port, and OpenD guidance.
- Missing watchlist file: exit nonzero with the missing path.
- Missing required watchlist columns: exit nonzero and list the columns.
- No monitorable US triggers: print a clear message and exit `0`.
- Quote failure for all symbols during startup: exit nonzero.
- Quote failure for one symbol during the loop: print a warning and continue.
- Invalid `--poll-seconds` or `--port`: reject at CLI parsing time.
- Ctrl-C: close the Futu connection and exit cleanly.

## Testing Strategy

Unit tests use fake quote clients and do not require Futu OpenD.

Coverage:

- Watchlist filtering keeps only active US price triggers.
- Invalid or unsupported watchlist rows are skipped.
- Missing required columns fail clearly.
- US symbols convert to `US.<symbol>`.
- `<=` and `>=` trigger evaluation is correct.
- Non-hit prices do not write alerts.
- Hit triggers append `alerts.csv` with the expected header and fields.
- Repeated hits for the same symbol and trigger alert only once per process.
- `--once` performs one quote fetch and exits.
- CLI wires paths, host, port, date, poll interval, and once mode correctly.
- Missing optional `futu-api` dependency is reported without a traceback.

Manual verification with real Futu OpenD:

```bash
.venv/bin/python -m open_trader watch-futu \
  --watchlist data/runs/2026-06-15/watchlist.csv \
  --data-dir data \
  --date 2026-06-15 \
  --poll-seconds 5 \
  --once
```

The command satisfies the first live-data milestone when it prints a successful
OpenD connection message and at least one `quote US.<SYMBOL> last_price=...`
line from a real Futu quote response.

## Non-Goals

- No order placement.
- No trade unlock.
- No account or position sync.
- No external notification channel.
- No HK stock monitoring.
- No Futu callback subscription loop in the first version.
- No automatic repair of ambiguous watchlist triggers.
