# Monthly Portfolio Import

Run this once per month after placing the latest broker statement PDFs on disk.

```bash
.venv/bin/python -m open_trader import-statements \
  --month 2026-05 \
  --futu /Users/ray/Downloads/futu.pdf \
  --tiger /Users/ray/Downloads/tiger.pdf \
  --phillips /Users/ray/Downloads/phillips.pdf \
  --usd-hkd 7.85
```

Update `--month` and `--usd-hkd` for the target statement month. Replace the PDF paths if the files are stored elsewhere.

Main output:

```text
data/latest/portfolio.csv
```

Trace outputs for the month:

```text
data/runs/<YYYY-MM>/manifest.csv
data/runs/<YYYY-MM>/extracted_positions.csv
data/runs/<YYYY-MM>/extracted_cash.csv
data/runs/<YYYY-MM>/parse_warnings.csv
data/runs/<YYYY-MM>/portfolio.csv
```

## Daily Premarket Advice

After `data/latest/portfolio.csv` exists, run the daily premarket advice workflow:

```bash
export DEEPSEEK_API_KEY=...

.venv/bin/python -m open_trader run-premarket \
  --date 2026-06-16 \
  --portfolio data/latest/portfolio.csv \
  --max-workers 3 \
  --ta-timeout-seconds 120 \
  --ta-max-retries 1 \
  --symbol-timeout-seconds 300
```

By default, the TradingAgents run uses DeepSeek:

```text
--ta-provider deepseek
--ta-deep-model deepseek-v4-pro
--ta-quick-model deepseek-v4-flash
--ta-timeout-seconds 120
--ta-max-retries 1
--symbol-timeout-seconds 300
```

Optional test run for a subset:

```bash
.venv/bin/python -m open_trader run-premarket \
  --date 2026-06-16 \
  --portfolio data/latest/portfolio.csv \
  --symbols VIXY,QQQ \
  --max-workers 2 \
  --ta-timeout-seconds 120 \
  --ta-max-retries 1 \
  --symbol-timeout-seconds 300 \
  --dry-run
```

Main readable output:

```text
reports/premarket/<YYYY-MM-DD>.md
```

Machine-readable action list:

```text
data/latest/premarket_actions.csv
```

## Build Watchlist

After the premarket run creates `data/latest/premarket_actions.csv`, convert it
into monitorable watchlist rows:

```bash
.venv/bin/python -m open_trader build-watchlist \
  --actions data/latest/premarket_actions.csv \
  --data-dir data \
  --date 2026-06-16
```

Use the same `--date` as the premarket run, normally the premarket date. The
command filters action rows to that date. If `--date` is omitted and the actions
file has headers but no rows, today's local date is used and a header-only
watchlist is written.

Optional dry run:

```bash
.venv/bin/python -m open_trader build-watchlist \
  --actions data/latest/premarket_actions.csv \
  --data-dir data \
  --date 2026-06-16 \
  --dry-run
```

Run output:

```text
data/runs/<YYYY-MM-DD>/watchlist.csv
data/latest/watchlist.csv
```

`--dry-run` writes only the dated run output and does not update
`data/latest/watchlist.csv`.

Rows with clear price conditions become `active`. Rows with unclear trigger text
become `manual_review` and should be reviewed before any future alerting
automation.

## Futu Quote Watch

Start Futu OpenD and log in before running the watcher. The first verification
mode fetches one quote snapshot and exits:

```bash
.venv/bin/python -m open_trader watch-futu \
  --watchlist data/runs/2026-06-15/watchlist.csv \
  --data-dir data \
  --date 2026-06-15 \
  --poll-seconds 5 \
  --once
```

Expected successful output includes:

```text
connected to Futu OpenD at 127.0.0.1:11111
loaded N active US trigger(s)
quote US.<SYMBOL> last_price=...
```

To keep watching until interrupted, omit `--once`.
