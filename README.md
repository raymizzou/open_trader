# Open Trader

[中文](README.zh-CN.md)

Open Trader is a local-first toolkit for turning broker statements and live
market data into structured portfolio records, premarket trading advice, and
reviewable action reports.

It is designed for a workflow where a human investor remains in control:
Open Trader reads data, calls analysis models, checks live quotes through Futu
OpenD, and writes reports. It does not place orders automatically.

## Features

- Import monthly broker statements into a normalized portfolio CSV.
- Generate per-symbol premarket advice with TradingAgents and DeepSeek.
- Preserve raw model output and normalized trader templates for auditability.
- Fall back to the latest prior successful advice when a daily run misses the
  hard deadline or a symbol analysis fails.
- Build machine-readable trading plans from the advice summaries.
- Check trading plans against live Futu OpenD quotes.
- Generate reviewable trade-action CSV and Markdown reports.
- Run the daily premarket workflow automatically on macOS with `launchd`.

## Safety Notice

This project is not financial advice and does not replace human review. Model
outputs can be incomplete, stale, or wrong. Always review generated advice,
plans, quote checks, and trade actions before making any investment decision.

Open Trader does not submit orders. Any order placement should remain a separate,
explicit, human-approved step.

## Quick Start

Create a Python 3.12 virtual environment and install the project:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

Prepare daily automation config:

```bash
cp config/daily_premarket.env.example config/daily_premarket.env
```

Edit `config/daily_premarket.env` and set your local values:

```env
DEEPSEEK_API_KEY=your-deepseek-api-key
OPEN_TRADER_REPO=/path/to/open_trader
OPEN_TRADER_PYTHON=/path/to/open_trader/.venv/bin/python
OPEN_TRADER_FUTU_HOST=127.0.0.1
OPEN_TRADER_FUTU_PORT=11111
```

Start Futu OpenD, log in, and confirm quote access:

```bash
.venv/bin/python -m open_trader check-futu-quotes \
  --portfolio data/latest/portfolio.csv
```

Run one daily premarket dry run:

```bash
.venv/bin/python -m open_trader run-daily-premarket \
  --date today \
  --config config/daily_premarket.env \
  --dry-run
```

Run one real daily check:

```bash
.venv/bin/python -m open_trader run-daily-premarket \
  --date today \
  --config config/daily_premarket.env
```

## Configuration

The local daily config file is:

```text
config/daily_premarket.env
```

This file is ignored by Git and should never be committed. The template is:

```text
config/daily_premarket.env.example
```

Important keys:

- `DEEPSEEK_API_KEY`: used by TradingAgents and the change classifier.
- `OPEN_TRADER_REPO`: absolute path to this repository.
- `OPEN_TRADER_PYTHON`: Python executable used by the scheduled job.
- `OPEN_TRADER_TIMEZONE`: defaults to `Asia/Shanghai`.
- `OPEN_TRADER_DEADLINE`: daily hard deadline, default `21:10`.
- `OPEN_TRADER_FUTU_HOST`: Futu OpenD host, usually `127.0.0.1`.
- `OPEN_TRADER_FUTU_PORT`: Futu OpenD quote port, usually `11111`.
- `OPEN_TRADER_CLASSIFIER_MODEL`: defaults to `deepseek-v4-flash`.

## Common Workflows

### Import Monthly Statements

```bash
.venv/bin/python -m open_trader import-statements \
  --month 2026-05 \
  --futu /path/to/futu.pdf \
  --tiger /path/to/tiger.pdf \
  --phillips /path/to/phillips.pdf \
  --usd-hkd 7.85
```

Main output:

```text
data/latest/portfolio.csv
```

### Run Premarket Advice Manually

```bash
.venv/bin/python -m open_trader run-premarket \
  --date 2026-06-16 \
  --portfolio data/latest/portfolio.csv \
  --max-workers 3 \
  --ta-timeout-seconds 120 \
  --ta-max-retries 1
```

### Build Trading Plan

```bash
.venv/bin/python -m open_trader build-trading-plan \
  --advice data/latest/trading_advice.csv \
  --data-dir data \
  --date 2026-06-16
```

### Check Futu Plan Quotes

```bash
.venv/bin/python -m open_trader check-futu-plan \
  --plan data/latest/trading_plan.csv
```

### Generate Trade Actions

```bash
.venv/bin/python -m open_trader generate-trade-actions \
  --plan data/latest/trading_plan.csv \
  --portfolio data/latest/portfolio.csv \
  --data-dir data \
  --reports-dir reports \
  --date 2026-06-16
```

## Daily Automation

Install the macOS user-level `launchd` job:

```bash
scripts/install_daily_premarket_launchd.sh
```

The job runs Monday through Friday at 18:30 Asia/Shanghai:

```text
.venv/bin/python -m open_trader run-daily-premarket --date today --config config/daily_premarket.env
```

The daily runner uses `21:10` Asia/Shanghai as the hard deadline. If a symbol
does not receive fresh advice before the deadline, the runner reuses the latest
prior successful advice for that symbol and marks it as `fallback`.

Uninstall the job:

```bash
scripts/uninstall_daily_premarket_launchd.sh
```

## Outputs

Run-scoped outputs:

```text
data/runs/<YYYY-MM-DD>/trading_advice.csv
data/runs/<YYYY-MM-DD>/change_classifications.csv
data/runs/<YYYY-MM-DD>/premarket_actions.csv
data/runs/<YYYY-MM-DD>/trading_plan.csv
data/runs/<YYYY-MM-DD>/daily_run_status.json
reports/daily_runs/<YYYY-MM-DD>.md
logs/daily_premarket/<YYYY-MM-DD>.log
```

Latest promoted outputs:

```text
data/latest/portfolio.csv
data/latest/trading_advice.csv
data/latest/premarket_actions.csv
data/latest/trading_plan.csv
```

## Development

Run the test suite:

```bash
.venv/bin/python -m pytest
```

Project entrypoint:

```bash
.venv/bin/python -m open_trader --help
```

Package CLI entrypoint:

```bash
open-trader --help
```

## License

TBD.
