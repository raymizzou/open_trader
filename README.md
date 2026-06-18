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
- Pull live Futu real-account holdings and cash into the standard portfolio CSV
  while keeping other brokers on statement imports.
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

Run daily premarket dry runs for each market:

```bash
.venv/bin/python -m open_trader run-daily-premarket \
  --market HK \
  --date today \
  --config config/daily_premarket.env \
  --dry-run

.venv/bin/python -m open_trader run-daily-premarket \
  --market US \
  --date today \
  --config config/daily_premarket.env \
  --dry-run
```

Run one real daily check for a market:

```bash
.venv/bin/python -m open_trader run-daily-premarket \
  --market HK \
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
- `OPEN_TRADER_DEADLINE`: US daily hard deadline, default `21:10`.
  HK daily runs use a fixed `09:00` Asia/Shanghai deadline.
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

If the command connects to `127.0.0.1:11111` but the snapshot call returns
`网络中断`, check whether OpenD is logged in to the quote server:

```bash
.venv/bin/python - <<'PY'
from futu import OpenQuoteContext
ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
ret, data = ctx.get_global_state()
print(ret, data)
ctx.close()
PY
```

Look for `qot_logined`. If `trd_logined=True` but `qot_logined=False`, the
trading server is logged in but the quote server is not. In that state,
`get_market_snapshot()` can return `网络中断`.

Restart OpenD completely and log in again:

```bash
ps aux | grep -i FutuOpenD
pkill -f FutuOpenD
ps aux | grep -i FutuOpenD
open /Applications/FutuOpenD_10.7.6718_Mac/FutuOpenD.app
```

After login, rerun `get_global_state()` and confirm `qot_logined=True`, then
rerun `check-futu-plan`. A healthy check prints `last_price=...` for active
plan symbols.

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

Install the macOS user-level `launchd` jobs:

```bash
scripts/install_daily_premarket_launchd.sh
```

By default this installs two jobs:

- `com.open-trader.premarket.hk`: Monday through Friday at 08:00 Asia/Shanghai.
- `com.open-trader.premarket.us`: Monday through Friday at 18:30 Asia/Shanghai.

Install only one market when needed:

```bash
scripts/install_daily_premarket_launchd.sh --market HK
scripts/install_daily_premarket_launchd.sh --market US
```

The scheduled commands run the daily workflow with an explicit market:

```text
.venv/bin/python -m open_trader run-daily-premarket --market HK --date today --config config/daily_premarket.env
.venv/bin/python -m open_trader run-daily-premarket --market US --date today --config config/daily_premarket.env
```

HK uses a fixed 09:00 Asia/Shanghai hard deadline so the premarket state is
available before the HK market opens. US uses `OPEN_TRADER_DEADLINE`, normally
21:10 Asia/Shanghai. If a symbol does not receive fresh advice before the
deadline, the runner reuses the latest prior successful advice for that symbol
and marks it as `fallback`.

Uninstall the job:

```bash
scripts/uninstall_daily_premarket_launchd.sh
```

## Outputs

Run-scoped outputs:

```text
data/runs/<YYYY-MM-DD>/HK/trading_advice.csv
data/runs/<YYYY-MM-DD>/HK/change_classifications.csv
data/runs/<YYYY-MM-DD>/HK/premarket_actions.csv
data/runs/<YYYY-MM-DD>/HK/trading_plan.csv
data/runs/<YYYY-MM-DD>/HK/trade_actions.csv
data/runs/<YYYY-MM-DD>/HK/daily_run_status.json
data/runs/<YYYY-MM-DD>/US/trading_advice.csv
data/runs/<YYYY-MM-DD>/US/change_classifications.csv
data/runs/<YYYY-MM-DD>/US/premarket_actions.csv
data/runs/<YYYY-MM-DD>/US/trading_plan.csv
data/runs/<YYYY-MM-DD>/US/trade_actions.csv
data/runs/<YYYY-MM-DD>/US/daily_run_status.json
reports/daily_runs/<YYYY-MM-DD>-HK.md
reports/daily_runs/<YYYY-MM-DD>-US.md
logs/daily_premarket/<YYYY-MM-DD>-HK.log
logs/daily_premarket/<YYYY-MM-DD>-US.log
```

Latest promoted outputs:

```text
data/latest/portfolio.csv
data/latest/HK/trading_advice.csv
data/latest/HK/premarket_actions.csv
data/latest/HK/trading_plan.csv
data/latest/HK/trade_actions.csv
data/latest/US/trading_advice.csv
data/latest/US/premarket_actions.csv
data/latest/US/trading_plan.csv
data/latest/US/trade_actions.csv
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
