# HK Market Support Design

## Goal

Add end-to-end Hong Kong market support while keeping HK and US daily workflows
separate.

The HK workflow must produce a usable premarket state before 09:00
Asia/Shanghai. For the immediate rollout, the first target run is before
2026-06-19 09:00 Asia/Shanghai. US workflow timing remains unchanged and keeps
its existing evening deadline.

No orders are placed by this feature.

## Decisions

- HK and US run as separate market-scoped daily workflows.
- Daily command accepts an explicit `--market HK|US` option.
- HK default deadline: 09:00 Asia/Shanghai.
- US default deadline: existing 21:10 Asia/Shanghai.
- HK and US write separate dated artifacts and separate latest artifacts.
- HK failures, fallback, readiness, Futu checks, and notifications do not affect
  the US run, and US failures do not affect the HK run.
- HK stocks and ETFs become AI-eligible portfolio rows. Cash, money market
  funds, options, unsupported markets, and malformed rows stay excluded.
- HK Futu symbols use `HK.<5-digit-code>`, for example `HK.00700`.
- Existing safety posture stays unchanged: machine-readable outputs are for
  human review, not automatic order placement.

## CLI Shape

Extend the daily runner:

```bash
.venv/bin/python -m open_trader run-daily-premarket \
  --market HK \
  --date today \
  --config config/daily_premarket.env

.venv/bin/python -m open_trader run-daily-premarket \
  --market US \
  --date today \
  --config config/daily_premarket.env
```

`--market` is required for the daily runner after this change. This avoids
accidentally running a mixed-market workflow or overwriting the wrong latest
artifact.

For compatibility, lower-level commands may keep their existing defaults, but
the market-scoped daily runner should call them through Python functions with an
explicit market filter.

## Artifact Layout

Market-scoped outputs are written under a market directory:

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
```

Latest artifacts are also market-scoped:

```text
data/latest/HK/trading_advice.csv
data/latest/HK/premarket_actions.csv
data/latest/HK/trading_plan.csv
data/latest/HK/trade_actions.csv

data/latest/US/trading_advice.csv
data/latest/US/premarket_actions.csv
data/latest/US/trading_plan.csv
data/latest/US/trade_actions.csv
```

Human-readable reports include the market in the file name:

```text
reports/premarket/<YYYY-MM-DD>-HK.md
reports/premarket/<YYYY-MM-DD>-US.md
reports/trade_actions/<YYYY-MM-DD>-HK.md
reports/trade_actions/<YYYY-MM-DD>-US.md
reports/daily_runs/<YYYY-MM-DD>-HK.md
reports/daily_runs/<YYYY-MM-DD>-US.md
```

Existing unscoped latest files can remain for older commands during migration,
but the market-scoped daily runner must read and promote only market-scoped
latest files.

## Scheduler

Install two user-level launchd jobs:

```text
com.open-trader.premarket.hk
com.open-trader.premarket.us
```

The HK job starts early enough to finish before 09:00 Asia/Shanghai. The default
start time should be 08:00 Asia/Shanghai with a hard deadline at 09:00. The US
job keeps the existing US schedule and deadline.

Suggested command shape:

```bash
.venv/bin/python -m open_trader run-daily-premarket --market HK --date today --config config/daily_premarket.env
.venv/bin/python -m open_trader run-daily-premarket --market US --date today --config config/daily_premarket.env
```

Installer and uninstaller scripts should manage both jobs or accept an explicit
market argument. The scripts must validate rendered plist files with
`plutil -lint`.

## Portfolio Eligibility

`portfolio.py` should mark these rows as AI eligible:

- `market=US`, `asset_class=stock|etf`
- `market=HK`, `asset_class=stock|etf`

HK rows keep `symbol` and `analysis_symbol` as the normalized broker symbol,
for example `00700`. Futu-facing code converts that value to `HK.00700`.

The portfolio sort order should keep AI-eligible rows first by market group, so
the daily reports remain scan-friendly.

## Market-Scoped Premarket Analysis

The premarket pipeline should accept a market filter and load only eligible rows
for that market.

HK prompt context must make the market explicit:

```text
Market: Hong Kong / HKEX
Currency: HKD
Symbol format: 00700 style portfolio symbol, HK.00700 Futu quote symbol
Deadline: before Hong Kong market open
```

The generated trader template remains structured in Chinese so the existing
trading-plan parser can extract rating, entry zone, add price, stop, target,
max weight, catalysts, and time window.

If HK analysis fails or reaches the 09:00 deadline, the runner applies the same
fallback rule as US: reuse the latest prior successful HK advice for the same
symbol when available, and write explicit fallback metadata.

## Trading Plan and Futu Quotes

`TradingPlanRow.futu_symbol` already supports HK digit symbols by returning
`HK.<symbol.zfill(5)>`. The implementation should add focused tests for HK
plans and ensure every quote-fetching path uses this property rather than
hand-building US symbols.

Futu quote checks must work for mixed Futu symbol prefixes at the lower layer,
but each daily run only fetches one market's active plans.

HK quote failures are reported in the HK `daily_run_status.json` and HK report
only.

## Trade Actions

Trade actions should support HK plans with HKD notional values:

- `notional_currency` is `HKD` for HK rows.
- `cash_available` is read from HKD cash rows.
- BUY and ADD sizing uses same-currency HKD cash, target maximum weight, and
  plan budget.
- TRIM, SELL_STOP, and TAKE_PROFIT work from the current HK position quantity.
- Any missing HKD cash or malformed HK row downgrades tradeable actions to
  `REVIEW`.

The CSV schema stays unchanged. Reports and Feishu messages should display HK
symbols and HKD amounts without adding prose-only fields.

## Watcher

`watch-futu` should support HK triggers as well as US triggers.

The trigger loader should accept `market=HK` rows and convert digit symbols to
`HK.<5-digit-code>`. It should continue to reject unsupported markets, malformed
symbols, inactive rows, unclear trigger types, and invalid trigger prices.

Watcher state and alert output remain dated. If market-scoped watchlists are
introduced, the HK watcher should read `data/latest/HK/watchlist.csv` and write
HK alerts under the matching dated run directory.

## Notifications

Daily notifications include the market in the title:

```text
Open Trader 港股盘前
Open Trader 美股盘前
Open Trader 港股阻塞通知
Open Trader 美股阻塞通知
```

Notification logs should record the market field so HK and US delivery attempts
can be audited independently.

HK notification failures must not change the US readiness state, and US
notification failures must not change the HK readiness state.

## Status and Readiness

Market-scoped `daily_run_status.json` includes:

```text
market: HK | US
run_date
started_at
finished_at
deadline_at
status
readiness
status_reasons
premarket
trading_plan
futu_plan_check
trade_actions
artifacts
notifications
```

Readiness is computed independently per market:

- `ready`: the market run produced explicit advice, plan, Futu check, and trade
  action state. Fallback rows may still be ready if they produce explicit,
  reviewable plans and actions.
- `review_required`: the market run completed but has review rows, missing
  quotes, or other non-fatal blockers.
- `blocked`: Futu, configuration, portfolio, or analysis failures prevent a
  usable market state.

## Error Handling

- Market-specific run locks prevent two HK jobs or two US jobs from overlapping.
- HK and US jobs may run on the same date without blocking each other.
- Dated market artifacts are written before market-scoped latest promotion.
- A failed HK run must not overwrite `data/latest/HK/*`.
- A failed HK run must never overwrite `data/latest/US/*`.
- Missing HK quotes do not fail the whole HK run; they mark affected rows
  `REVIEW` and appear in the HK status/report.
- Unsupported HK assets remain excluded rather than guessed.

## Tests

Add focused tests for:

- HK stock and ETF rows becoming `ai_eligible=true`.
- HK cash, money market funds, malformed symbols, and unsupported assets staying
  excluded.
- Market-scoped portfolio loading for HK and US.
- HK daily runner paths and latest promotion paths.
- HK deadline defaulting to 09:00 while US keeps 21:10.
- HK fallback lookup using only prior HK advice.
- HK trading plan `HK.00700` Futu symbol generation.
- HK Futu quote checks and missing-quote handling.
- HK trade-action sizing with HKD cash.
- HK trade-action review downgrade when HKD cash or required position fields are
  missing.
- HK watcher trigger loading and `HK.<5-digit-code>` conversion.
- HK and US notification titles and notification logs.
- Migration behavior that does not overwrite existing unscoped artifacts.

Manual verification should include one dry run for HK and one dry run for US,
then one real HK run with Futu OpenD connected before the HK deadline.

## Out of Scope

- Automatic order placement.
- Broker trade submission.
- Cross-market portfolio optimization.
- Currency conversion for order buying power beyond same-currency cash checks.
- A web dashboard.
- Guaranteeing model quality for every HK symbol. The workflow must surface
  failures and fallback explicitly instead of hiding uncertainty.
