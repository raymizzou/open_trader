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
- Pull live Tiger OpenAPI holdings and cash into the standard portfolio CSV
  while preserving non-Tiger rows.
- Generate per-symbol premarket advice with TradingAgents and DeepSeek.
- Preserve raw model output and normalized trader templates for auditability.
- Extract K-line technical facts from TradingAgents advice/report output into
  cacheable `technical_facts.json` artifacts.
- Fall back to the latest prior successful advice when a daily run misses the
  hard deadline or a symbol analysis fails.
- Build machine-readable trading plans from the advice summaries.
- Check trading plans against live Futu OpenD quotes.
- Generate reviewable trade-action CSV and Markdown reports.
- View a local realtime portfolio dashboard with live quote refresh and stale
  data warnings.
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

Tiger OpenAPI account sync uses Tiger's official
`~/.tigeropen/tiger_openapi_config.properties` file, CLI `--account`, or
`TIGEROPEN_*` environment variables. Supported environment variables are
`TIGEROPEN_TIGER_ID`, `TIGEROPEN_ACCOUNT`, `TIGEROPEN_PRIVATE_KEY_PATH`,
`TIGEROPEN_PRIVATE_KEY`, and optional `TIGEROPEN_SECRET_KEY` or
`TIGEROPEN_TOKEN`. Prefer the properties file or private key path over a raw
private key environment value.

## Common Workflows

### Import Monthly Statements

```bash
.venv/bin/python -m open_trader import-statements \
  --month 2026-05 \
  --phillips /path/to/phillips.pdf \
  --usd-hkd 7.85
```

Main output:

```text
data/latest/portfolio.csv
```

Futu and Tiger current holdings are refreshed through live account sync
commands, not monthly statement import.

### Run Premarket Advice Manually

```bash
.venv/bin/python -m open_trader run-premarket \
  --date 2026-06-16 \
  --portfolio data/latest/portfolio.csv \
  --max-workers 3 \
  --ta-timeout-seconds 120 \
  --ta-max-retries 1
```

Premarket advice runs extract K-line technical facts from each TradingAgents
advice/report row after advice generation. The cache is written as
`technical_facts.json`; daily market-scoped runs promote it with the rest of the
latest set when the run is successful.

### Backfill Technical Facts

Use the extractor CLI when you need to rebuild technical facts from an existing
advice CSV:

```bash
open-trader extract-technical-facts \
  --advice data/runs/2026-06-19/US/trading_advice.csv \
  --data-dir data \
  --date 2026-06-19 \
  --market US \
  --update-latest
```

With `--market HK` or `--market US`, the dated cache is written under that
market's run directory and `--update-latest` promotes it to the matching latest
market path, such as `data/latest/HK/technical_facts.json` or
`data/latest/US/technical_facts.json`.

### Fixed decision facts

After a market-scoped TradingAgents run, Open Trader extracts fixed Chinese
decision fields for the dashboard:

```text
data/runs/<YYYY-MM-DD>/<MARKET>/decision_facts.json
data/latest/<MARKET>/decision_facts.json
```

Manual command:

```bash
.venv/bin/python -m open_trader extract-decision-facts \
  --advice data/latest/US/trading_advice.csv \
  --data-dir data \
  --date 2026-06-22 \
  --market US \
  --update-latest
```

The dashboard uses fixed fields for `Trend / K-line` and `News / Sentiment`.
Every symbol uses the same field layout:

- `Trend / K-line`: `趋势`, `位置`, `动能`, `关键位`, `风险`
- `News / Sentiment`: `方向`, `变化`, `催化`, `风险`, `热度`

Missing field values are rendered as `缺失`; the dashboard does not display raw
English TradingAgents prose, generic "not mentioned" explanations, or manual
review filler in those plugin fields.

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

### Sync Tiger OpenAPI Portfolio

Tiger live account sync is read-only and does not place orders. Real holdings
and cash come directly from Tiger OpenAPI, not from `portfolio.csv`.
`data/latest/portfolio.csv` is only the default merge baseline: when present it
preserves non-Tiger rows and replaces Tiger-only rows; when missing the sync
still writes a dated broker-only portfolio from live Tiger data. Monthly
`import-statements` handles brokers that still rely on statements; Tiger sync is
the current-account refresh path.

```bash
.venv/bin/python -m open_trader check-tiger-account

.venv/bin/python -m open_trader sync-tiger-portfolio \
  --date 2026-06-19
```

The sync command above is the no-latest review run by default. Review
`data/runs/2026-06-19/tiger_account_snapshot.json`,
`data/runs/2026-06-19/portfolio.csv`, and
`reports/tiger_account/2026-06-19.md`. Then promote after review:

```bash
.venv/bin/python -m open_trader sync-tiger-portfolio \
  --date 2026-06-19 \
  --update-latest
```

Rows that mix Tiger with another broker stop for manual review instead of
being split automatically. Malformed Tiger data writes dated artifacts and a
report, then blocks latest promotion.

### Generate Trade Actions

```bash
.venv/bin/python -m open_trader generate-trade-actions \
  --plan data/latest/trading_plan.csv \
  --portfolio data/latest/portfolio.csv \
  --data-dir data \
  --reports-dir reports \
  --date 2026-06-16
```

### Deploy Local Frontend Dashboard

```bash
.venv/bin/python -m open_trader dashboard \
  --portfolio data/latest/portfolio.csv \
  --data-dir data \
  --reports-dir reports \
  --poll-seconds 5 \
  --host 127.0.0.1 \
  --port 8766
```

The dashboard serves locally at `http://127.0.0.1:8765` by default; the example
above pins it to `http://127.0.0.1:8766` so the local deployment URL stays
stable. It reads
`data/latest/portfolio.csv`, broker detail artifacts under
`data/broker_positions/`, and the latest trade actions and reports when present.

When Futu OpenD quotes are available, the dashboard refreshes prices from OpenD.
If a quote refresh fails, it keeps the last successful quote snapshot and shows
a failure or stale warning instead of hiding the problem.

The dashboard also reads technical facts from `technical_facts.json` when
available. Symbol details show both the facts run date and the underlying market
data date. Missing files, missing records, stale source hashes, extraction
errors, or incomplete timeframe data are marked unavailable, so stale technical
facts are not presented as current.

The dashboard is read-only: it does not place orders or modify data.

### Research Chat Workflow

The dashboard can display a TradingAgents research bundle for each holding when
the bundle exists under `data/research_data/<market>/<symbol>/<date>/`.

Required bundle files:

- `dashboard_view.json`: dashboard-facing conclusions.
- `combined_input.json`: raw TradingAgents output plus local user context.
- `llm_system_prompt.md`: the system prompt loaded automatically when chat starts.

The symbol detail page shows two conclusion cards:

- `投研给出的结论`: the original TradingAgents conclusion.
- `我和 LLM 探讨后的结论`: missing until the user clicks `生成最终结论` in chat.

Chat transcripts are stored under `data/research_chat/sessions/`. Finalization
writes `user_llm_conclusion.json` into the research bundle and updates that
bundle's `dashboard_view.json`. This workflow is read-only for trading: it does
not place orders and does not modify trade action files.

To keep the local frontend running after the terminal closes, start it in a
detached `screen` session:

```bash
screen -S open_trader_dashboard_8766 -X quit 2>/dev/null || true

screen -dmS open_trader_dashboard_8766 zsh -lc \
  'cd /Users/ray/projects/open_trader && export PYTHONPATH=src && exec .venv/bin/python -m open_trader dashboard --portfolio data/latest/portfolio.csv --data-dir data --reports-dir reports --poll-seconds 5 --host 127.0.0.1 --port 8766'
```

Verify the deployment:

```bash
curl -sS http://127.0.0.1:8766/ | head
curl -sS http://127.0.0.1:8766/api/dashboard | head -c 500
ps aux | rg 'open_trader dashboard'
```

For a structured API check, run:

```bash
.venv/bin/python - <<'PY'
import json
from urllib.request import urlopen

with urlopen("http://127.0.0.1:8766/api/dashboard", timeout=10) as response:
    payload = json.load(response)

print("holding_count", payload.get("summary", {}).get("holding_count"))
print("detail_available", payload.get("detail_available"))
print(
    "has_soxx_decision_facts",
    any(
        holding.get("market") == "US"
        and holding.get("symbol") == "SOXX"
        and bool(holding.get("decision_facts"))
        for holding in payload.get("holdings", [])
    ),
)
PY
```

Stop the detached dashboard when needed:

```bash
screen -S open_trader_dashboard_8766 -X quit
```

## Daily Automation

### Deploy Daily Premarket Jobs

Install the macOS user-level `launchd` jobs:

```bash
scripts/install_daily_premarket_launchd.sh --dry-run --market all
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

Verify the scheduled deployment:

```bash
launchctl list | rg 'open-trader|premarket'
plutil -lint \
  ~/Library/LaunchAgents/com.open-trader.premarket.hk.plist \
  ~/Library/LaunchAgents/com.open-trader.premarket.us.plist
```

Expected loaded labels:

```text
com.open-trader.premarket.hk
com.open-trader.premarket.us
```

Check scheduler logs after a run:

```bash
tail -n 100 logs/daily_premarket/launchd-HK.out.log
tail -n 100 logs/daily_premarket/launchd-HK.err.log
tail -n 100 logs/daily_premarket/launchd-US.out.log
tail -n 100 logs/daily_premarket/launchd-US.err.log
```

## Outputs

Run-scoped outputs:

```text
data/runs/<YYYY-MM-DD>/HK/trading_advice.csv
data/runs/<YYYY-MM-DD>/HK/change_classifications.csv
data/runs/<YYYY-MM-DD>/HK/premarket_actions.csv
data/runs/<YYYY-MM-DD>/HK/trading_plan.csv
data/runs/<YYYY-MM-DD>/HK/trade_actions.csv
data/runs/<YYYY-MM-DD>/HK/technical_facts.json
data/runs/<YYYY-MM-DD>/HK/decision_facts.json
data/runs/<YYYY-MM-DD>/HK/daily_run_status.json
data/runs/<YYYY-MM-DD>/US/trading_advice.csv
data/runs/<YYYY-MM-DD>/US/change_classifications.csv
data/runs/<YYYY-MM-DD>/US/premarket_actions.csv
data/runs/<YYYY-MM-DD>/US/trading_plan.csv
data/runs/<YYYY-MM-DD>/US/trade_actions.csv
data/runs/<YYYY-MM-DD>/US/technical_facts.json
data/runs/<YYYY-MM-DD>/US/decision_facts.json
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
data/latest/HK/technical_facts.json
data/latest/HK/decision_facts.json
data/latest/US/trading_advice.csv
data/latest/US/premarket_actions.csv
data/latest/US/trading_plan.csv
data/latest/US/trade_actions.csv
data/latest/US/technical_facts.json
data/latest/US/decision_facts.json
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

Before pushing `main`, add one dated entry to `CHANGELOG.md` for the change
being pushed. The entry should summarize user-visible behavior, affected
workflows, and verification.

## License

TBD.
