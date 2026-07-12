# Standard Strategy Backtest MVP Design

## Summary

Replace the current holding-row Trading Plan backtest experience with one global
`策略回测` entry on the dashboard homepage. The user selects one holding or
watchlist symbol, one of three versioned daily-swing strategies, and a historical
range. Open Trader translates that strategy into a stable execution contract,
runs it through Backtrader, and compares the result with both buy-and-hold and a
market benchmark.

This MVP is an on-demand research tool. It does not generate daily production
signals, monitor strategies continuously, place orders, recommend strategies,
or attempt to become a general-purpose backtesting platform.

## User and Product Goals

The primary user is comfortable choosing an investment idea but is not expected
to understand quantitative-finance implementation details. The product must:

- offer three understandable, standardized strategies;
- let the user decide which strategy to test on which symbol;
- keep the strategy definition independent from the execution tool;
- make backtest dates, assumptions, transactions, and benchmarks explicit;
- preserve a future `自定义策略` entry without implementing it in this MVP.

## Scope

### Included

- One global dashboard entry for backtesting.
- Symbols from current holdings and the existing watchlist.
- One symbol and one strategy per run.
- Three preset daily-swing strategies:
  - `趋势回调`
  - `突破动量`
  - `区间均值回归`
- Standard actions: `BUY`, `ADD`, `HOLD`, `REDUCE`, and `EXIT`.
- Quick historical ranges of 6 months, 1 year, 3 years, and 5 years.
- A custom start date and optional custom end date.
- Backtrader as the hidden default execution adapter.
- Buy-and-hold and market-index benchmarks.
- Versioned strategy definitions and reproducible run artifacts.

### Excluded

- Per-holding-row backtest buttons and homepage backtest-status filters.
- Daily signal generation, scheduled monitoring, notifications, and live orders.
- Market-regime detection or strategy suitability judgments.
- Automatic strategy recommendation or switching.
- Multi-symbol comparison and portfolio backtests.
- Custom strategy editing or a strategy programming language.
- Parameter search, optimization, vectorbt selection, and walk-forward tooling.

## Primary User Flow

1. The user clicks the single `策略回测` action in the dashboard header.
2. The backtest workspace opens as a separate page or full workspace, not as a
   holding-row detail.
3. The user chooses `当前持仓` or `自选股`, then selects one symbol.
4. The user selects one of the three strategy cards.
5. The user selects a quick range or enters a custom range.
6. The user reviews maximum strategy weight, initial capital, commission, and
   slippage. Defaults are provided.
7. The user runs the backtest.
8. The result view displays strategy results, both benchmarks, charts, trades,
   the actual data range, and the exact strategy version and parameters.

The homepage holding rows retain their existing `交易决策` and `做 T` actions.
They do not contain any backtest action.

## Time-Range Rules

- The default end date is the symbol's latest available trading date.
- Quick ranges are calculated backward from that effective end date.
- Custom mode accepts a required start date and an optional end date.
- The result records both the requested range and the actual available range.
- Indicator warm-up data may be read before the requested start date, but it
  cannot produce trades or contribute to performance.
- A decision based on day `T` closing data can execute no earlier than day
  `T+1`. The adapter must not trade on data that was unavailable at decision
  time.

## Common Position and Action Contract

The user sets a `max_strategy_weight`. The strategy emits target weights rather
than share quantities so an external adapter can perform its own sizing.

| Action | Target weight |
| --- | ---: |
| `BUY` | 50% of `max_strategy_weight` |
| `ADD` | 100% of `max_strategy_weight` |
| `HOLD` | unchanged |
| `REDUCE` | 50% of `max_strategy_weight` |
| `EXIT` | 0% |

Only one action is emitted per symbol per bar. When multiple conditions are
true, precedence is `EXIT`, `REDUCE`, `ADD`, `BUY`, then `HOLD`.

Each simulated signal contains:

- symbol and market;
- strategy identifier and version;
- decision date and earliest execution date;
- action and target weight;
- triggered rule and human-readable explanation;
- input-data cutoff;
- parameters used for the run.

## Preset Strategy Definitions

These definitions are fixed as version `v1` defaults. They are hypotheses for
historical testing, not promises of investment performance. The MVP exposes the
maximum strategy weight but does not require the user to edit indicator values.

### Trend Pullback (`trend_pullback/v1`)

- Indicators: SMA20, SMA50, ATR14, and RSI14.
- `BUY`: no position; SMA20 is above SMA50; close is above SMA50; the day's low
  touches or falls below SMA20; and the close finishes above SMA20.
- `ADD`: the position is at the initial target; the trend conditions remain
  true; and the close exceeds the highest close of the preceding five sessions.
- `REDUCE`: the position is at full target and either RSI14 is at least 75 or
  the close is at least two ATR14 above SMA20.
- `EXIT`: close is below SMA50 or below the active stop, set two ATR14 below the
  most recent entry or add execution price.

### Breakout Momentum (`breakout_momentum/v1`)

- Indicators: prior 20-session high, 20-session average volume, and ATR14.
- `BUY`: no position; close exceeds the preceding 20-session high; and volume is
  at least 1.5 times preceding 20-session average volume.
- `ADD`: the position is at the initial target; close is at least one ATR14
  above the initial execution price; and close remains above the breakout level.
- `REDUCE`: the position is at full target and close falls below SMA10 while
  remaining above the breakout level.
- `EXIT`: close falls below the stored breakout level or below the active stop,
  set two ATR14 below the most recent entry or add execution price.

### Range Mean Reversion (`range_mean_reversion/v1`)

- Indicators: 20-session Bollinger Bands at two standard deviations, RSI14,
  and ATR14.
- `BUY`: no position; close is at or below the lower band; and RSI14 is at most
  30.
- `ADD`: the position is at the initial target and a later close recovers above
  the lower band without first triggering the active stop.
- `REDUCE`: the position is at full target and close reaches the middle band.
- `EXIT`: close reaches the upper band or falls below the active stop, set two
  ATR14 below the initial execution price.

The system does not decide whether the selected strategy suits the current
market environment. If no action rule is triggered, the historical action for
that bar is `HOLD`.

## Strategy and Adapter Boundary

The strategy layer receives point-in-time OHLCV bars and returns standard target
weight signals. It does not call Backtrader APIs directly.

The adapter layer:

- converts target weights into orders and quantities;
- applies next-session execution timing;
- models commission and slippage;
- produces normalized trades, equity rows, and metrics;
- reports adapter failures separately from strategy failures.

Backtrader is the only MVP adapter and is not shown as a user choice. A future
vectorbt or external-platform adapter must consume the same versioned strategy
and signal contract.

## Benchmarks and Fair Comparison

Every successful run includes:

1. the selected strategy;
2. buy-and-hold for the same symbol and period;
3. a market benchmark for the same period.

The default US benchmark is SPY. The default HK benchmark is Tracker Fund of
Hong Kong (`HK.02800`). Both are repository constants and are displayed in the
run form and result view.

All three curves use the same initial capital, effective date range, cost
assumptions, and maximum allocated notional. Unallocated capital remains cash so
the strategy and benchmarks are compared on the same capital basis. The result
also reports the strategy's excess return against each benchmark.

## Result View

The result view contains:

- total and annualized return;
- buy-and-hold and market-index returns;
- excess return against both benchmarks;
- maximum drawdown;
- trade count and win rate;
- strategy, buy-and-hold, and market-index equity curves;
- price chart with `BUY`, `ADD`, `REDUCE`, and `EXIT` markers;
- normalized trade detail;
- requested and actual data ranges;
- initial capital, maximum strategy weight, commission, and slippage;
- strategy identifier, version, and fixed parameters;
- artifact paths and a stable run identifier.

A successful run with zero trades is displayed as a valid result with an
explicit `所选区间内没有触发交易` explanation, not as a system error.

## Errors and Data Quality

The UI distinguishes:

- insufficient warm-up or in-range price data;
- requested dates outside available data;
- stale or malformed price data;
- invalid user inputs;
- missing benchmark data;
- adapter execution failure.

The system never silently shortens the requested period without showing the
actual period. Missing benchmark data does not alter strategy metrics; it marks
the affected comparison unavailable and explains why.

## Storage and Reproducibility

Each run stores an immutable manifest containing:

- run identifier and creation time;
- requested and actual date ranges;
- symbol and market;
- price and benchmark source hashes;
- strategy identifier, version, and parameters;
- adapter name and version;
- capital and cost assumptions;
- normalized result artifact paths.

Changing a preset strategy requires a new strategy version. Existing results
continue to reference the original version.

## Verification

Automated verification must cover:

- all five actions for every preset strategy;
- action precedence and target-weight transitions;
- next-session execution and warm-up exclusion;
- no use of bars after a decision timestamp;
- quick and custom date ranges;
- identical effective ranges and capital bases for both benchmarks;
- zero-trade runs;
- missing, stale, and malformed price or benchmark data;
- adapter failure isolation;
- API payloads and dashboard rendering.

Before completion, run the focused tests and full relevant test suite. Then run
the real dashboard workflow against current local holdings and watchlist data,
restart any stale dashboard process, and verify the single homepage entry,
absence of per-row backtest actions, form submission, and result rendering in a
browser against the live local service.
