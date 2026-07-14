# Tiger Long-Term Portfolio Strategy Design

## Goal

Add a shadow-mode long-term portfolio strategy for the Tiger account. The
strategy manages the full Tiger account NAV across a manually maintained pool
of US stocks and ETFs, uses a deterministic SMA200 long/cash signal, and is
judged primarily by out-of-sample Sharpe and Calmar ratios rather than raw
benchmark outperformance.

This phase builds the strategy, portfolio backtest, validation artifact, and
Dashboard evidence. It does not place orders or claim to validate stock
selection.

## Scope

The account roles are fixed for this phase:

- Tiger is the long-term strategy account and supplies 100% of the strategy
  capital base.
- Futu remains the medium/short-term and options account.
- Phillips remains the medium-term account.
- Futu and Phillips positions and cash do not enter this strategy's NAV,
  targets, backtest, or rebalance calculations.

The initial Tiger strategy pool is seeded from the current ordinary US stock
and ETF holdings. It is stored persistently and maintained manually. Selling a
member to zero does not remove it. Options, cash rows, account reconciliation
rows, automatic stock discovery, ranking, and fundamental selection are out of
scope.

The checked-in version 1 pool and risk groups are:

| Symbol | Risk group |
| --- | --- |
| `DRAM` | `semiconductor` |
| `SOXX` | `semiconductor` |
| `EUV` | `semiconductor` |
| `TSM` | `semiconductor` |
| `SMH` | `semiconductor` |
| `MSFT` | `software` |
| `QQQ` | `broad_us_growth` |
| `AGRZ` | `agriculture` |

Membership does not imply eligibility. New or illiquid members such as those
without six years of valid history remain visible and fail eligibility rather
than being dropped.

Historical results must be labelled `conditional_on_current_universe`. They
test the timing and allocation overlay conditional on today's manually chosen
pool; they do not test whether the system could have selected those securities
in the past.

## Approaches Considered

### Keep the existing single-symbol plans

This preserves the smallest code surface, but independent 10% targets can sum
to more than 100% and cannot decide which simultaneous signals receive cash.
It cannot produce a coherent account-level rebalance.

### Add a generic multi-broker portfolio engine

This could eventually support all account styles, but it mixes unrelated
long-term, medium-term, short-term, and options mandates before any one mandate
has been validated.

### Add a Tiger-only long-term portfolio seam

This is the chosen approach. It introduces one account-level strategy artifact
and reuses the existing Tiger holdings, price cache, Backtrader assumptions,
Dashboard loading patterns, immutable run artifacts, and acceptance gate. The
boundary may be generalized only after another account has a concrete strategy
with the same semantics.

## Strategy Rule

The first strategy version is `tiger_sma200_equal_weight/v1`.

For each eligible pool member:

- calculate SMA200 from completed, adjusted daily closes;
- emit `LONG` when the completed close is strictly above SMA200;
- emit `CASH` when the completed close is at or below SMA200;
- execute any state change at the next session's open;
- use no Bollinger Bands, candlestick patterns, RSI, take-profit, leverage,
  shorting, or per-symbol parameter optimization.

The signal parameters are identical across every pool member. A future signal
variant must receive a new version and be evaluated as a separate challenger;
it may not silently change version 1.

## Eligibility and Market Data

A pool member is eligible only when all of these are true:

- it is an ordinary US stock or unleveraged, non-inverse US ETF;
- adjusted OHLCV history covers at least one warm-up year followed by five
  complete evaluation years;
- the source identifies its corporate-action adjustment method;
- dividends are included in strategy and benchmark returns through the same
  adjustment series;
- dates are ordered and unique and required price fields are finite;
- the evaluation period has no unexplained material data gap.

An ineligible member remains visible with a reason and receives no target
weight. Missing adjustment or dividend provenance is a hard failure, not a
zero-filled assumption.

The first version uses Futu historical daily K lines with `AuType.QFQ`
explicitly requested and records a hash of Futu's `get_rehab` adjustment data.
Futu adjustment factors include cash dividends and other corporate actions, so
the simulator must not credit a second cash dividend on top of QFQ prices. The
stored validation artifact records source hashes, `futu_qfq` adjustment mode,
`included_in_qfq` dividend treatment, data cutoff, and cash-rate source. The
existing unlabelled OHLCV cache is not sufficient evidence by itself for a
passing validation. See the official Futu documentation for
[`request_history_kline`](https://openapi.futunn.com/pdfs/Futu-API-Doc-en-Python.pdf)
and [`get_rehab`](https://openapi.futunn.com/futu-api-doc/en/quote/get-rehab.html).

## Portfolio Allocation

The strategy capital base is the Tiger account's full net liquidation value.
Targets are calculated only from eligible `LONG` members:

1. If there are ten or fewer `LONG` members, assign 10% to each.
2. If there are more than ten, assign `100% / member_count` to each.
3. Cap each symbol at 10% of Tiger NAV.
4. Apply a 30% cap to each manually assigned `risk_group` by scaling all
   members of an over-limit group proportionally.
5. Leave every unallocated amount in interest-bearing USD cash.

The initial configuration assigns risk groups manually. The framework does not
rank members within a group. This intentionally leaves cash when the pool is
small, few members are in a positive trend, or a risk group is concentrated.

The daily calculation always publishes target and actual weights, but emits a
rebalance item only when:

- a member changes between `LONG` and `CASH`;
- a symbol exceeds the 10% hard cap;
- a risk group exceeds the 30% hard cap; or
- actual weight differs from target by more than two percentage points.

No automated broker order is created. Rebalances are review-only instructions.

Live NAV and actual positions come from the current day's immutable
`data/runs/<date>/tiger_account_snapshot.json` for account alias `tiger_5683`,
using its account-total/net-liquidation record and account-scoped position
records. The aggregated `data/latest/portfolio.csv` is not a valid NAV source
because identical cash symbols may be merged across Tiger, Futu, and Phillips.

## Portfolio Backtest

The portfolio backtest standardizes the strategy and benchmark to 100% of a
standalone Tiger strategy account. The live 10% symbol cap remains part of the
portfolio allocator; metrics are not diluted by unrelated Futu or Phillips
capital.

The simulator must:

- use at least one warm-up year without counting it in returns;
- evaluate the following five years in non-overlapping six-month segments;
- compute signals only from information available at each completed close;
- transact at the following common session's open;
- apply the same Tiger US-stock fee schedule and slippage assumptions to
  strategy and benchmark;
- credit idle USD cash from the Federal Reserve's daily three-month Treasury
  constant-maturity series used to calculate excess-return Sharpe;
- reflect dividends and corporate actions through the verified QFQ adjustment
  series without double-counting cash distributions;
- reject missing inputs instead of substituting fixtures or zero values.

The primary benchmark uses the same pool, symbol caps, risk-group caps, cash
model, cost model, and initial allocation, but always keeps eligible members
long instead of applying SMA200. SPY buy-and-hold is secondary market context
and is not a gate requirement.

The output includes annualized return, maximum drawdown, excess-return Sharpe,
Calmar, time in market, completed round trips, per-trade and per-symbol profit
contribution, turnover, costs, and all six-month segment metrics for the
strategy and primary benchmark.

The version 1 US transaction-cost model follows Tiger Brokers Hong Kong's
published online US-stock schedule as observed on 2026-07-14:

- commission: USD `0.0049` per share, minimum USD `0.99` per order, capped at
  `0.5%` of trade value;
- platform fee: USD `0.005` per share, minimum USD `1` per order, capped at
  `0.5%` of trade value;
- settlement fee: USD `0.003` per share, rounded to cents, capped at `7%` of
  trade value;
- sell-only SEC fee: `0.0000206 * trade value`, minimum USD `0.01`;
- sell-only FINRA activity fee: USD `0.000195` per share, minimum USD `0.01`,
  maximum USD `9.79`;
- execution slippage: `5` basis points on each buy and sell.

An order below one share instead uses Tiger's published fractional-share rule:
zero commission and third-party fees plus a platform fee of `1%` of trade value
capped at USD `1`. Fee changes require a new cost-model version and invalidate
cached validation. The source is Tiger's official
[`Commission and Fees`](https://www.itiger.com/hk/en/commissions?lang=en_US)
page.

USD cash uses FRED series `DGS3MO`, whose source is the Board of Governors of
the Federal Reserve System. For each interval between portfolio valuation
dates, use the latest observation on or before the interval start and accrue
`(1 + rate / 100) ** (calendar_days / 365) - 1`. A missing observation is
forward-filled only from an earlier published observation; no future rate may
be backfilled. The source URL and downloaded-series hash are stored in the
artifact. See the official
[`DGS3MO` series](https://fred.stlouisfed.org/series/dgs3mo).

## Validation Gate

The combined five-year out-of-sample curve is the primary evidence. Individual
six-month segments are diagnostics and do not each have veto power.

The fixed gate requirements are:

- strategy Sharpe is at least `0.8` and no lower than the primary benchmark;
- strategy Calmar is at least `0.8` and no lower than the primary benchmark;
- strategy annualized return is greater than the modeled cash return;
- strategy maximum drawdown is no worse than the primary benchmark;
- every required data, cost, dividend, cash, and provenance check passes.

The artifact also calculates the agreed anti-degeneracy evidence: completed
round trips, average invested weight, distinct traded members, and maximum
single-trade profit contribution. Their final numeric limits must be calibrated
from the first real Tiger run rather than selected to make the strategy pass.
Until a follow-up version freezes those limits, the gate reports
`calibration_required` and cannot produce an active strategy. This is an
explicit first-framework state, not a placeholder or implicit pass.

Recent one-year results are displayed as degradation evidence. They do not
override the combined five-year result for an ordinary losing period, but a
future gate version may define an explicit catastrophic-degradation rule after
the initial distribution is available.

## Runtime and Artifacts

The framework writes immutable and latest artifacts:

```text
config/tiger_long_term_strategy.json
data/runs/<YYYY-MM-DD>/US/tiger_long_term_strategy.json
data/latest/US/tiger_long_term_strategy.json
```

The configuration contains the strategy version and manual `symbol ->
risk_group` pool only. Financial thresholds remain versioned code constants so
changing them cannot silently reinterpret an old artifact.

The artifact contains:

- schema and strategy versions;
- run date, price cutoff, account alias, and Tiger NAV;
- shadow/validated status and structured gate reasons;
- the `conditional_on_current_universe` limitation;
- member eligibility, trend state, actual weight, target weight, drift, and
  rebalance reason;
- portfolio and benchmark metrics;
- six-month segment metrics and anti-degeneracy diagnostics;
- source, adjustment, dividend, cash-rate, cost, and request hashes.

Signal and account calculations run daily. The full five-year validation is
reused for one calendar month unless strategy code, configuration, price data,
cash-rate data, cost assumptions, or provenance changes. Any such change
invalidates the cached validation immediately. A missing, stale, corrupt, or
failed validation leaves the strategy in shadow mode.

## Dashboard

The Tiger long-term panel shows:

- shadow, calibration-required, failed, or validated status;
- strategy and primary-benchmark Sharpe, Calmar, return, and drawdown;
- the SPY context metrics;
- the manual-pool and current-universe limitation;
- per-member trend, eligibility, risk group, actual/target weight, and drift;
- review-only rebalance items;
- exact gate failures and source provenance.

The existing single-symbol backtest evidence remains available but no longer
determines this Tiger portfolio strategy's status. Futu and Phillips retain
their current Dashboard roles and are not labelled as participants in the
long-term strategy.

## Failure Handling

- Missing Tiger account data: publish a generation failure without reusing an
  old NAV.
- Missing or invalid member market data: retain the member as ineligible and
  identify the cause; do not silently remove it.
- Missing benchmark, cash rate, dividend, adjustment, or cost evidence: fail
  validation and remain in shadow mode.
- A member with no next-session execution bar: do not fabricate an execution.
- A failed monthly validation: retain its immutable artifact for diagnosis but
  do not issue active rebalance instructions.
- A stale latest artifact: display its cutoff and block active status.

## Verification and Deployment

Implementation follows test-first red/green cycles at the strategy signal,
allocation, portfolio simulation, gate, artifact, API, and browser seams.

Before review:

1. Run focused automated tests and the full relevant suite.
2. Run the real Tiger shadow workflow against current account and market data.
3. Inspect any running Dashboard process and restart code held in memory.
4. Run `make acceptance` as the final verification gate.
5. Only if acceptance returns `PASS`, redeploy the exact accepted Git SHA.
6. Verify the new PID, working directory, Git SHA, fresh logs, and HTTP 200 from
   the review URL before asking for review.

`FAIL` must be fixed and retested. `BLOCKED` must be reported as blocked and
cannot be replaced by mocks, fixtures, curl, or screenshots.

## Explicitly Deferred

- stock selection and automatic pool discovery;
- Bollinger Bands, candlestick patterns, and signal ensembles;
- per-symbol parameter fitting;
- Futu medium/short-term and options strategies;
- Phillips medium-term strategy;
- automatic order placement or broker allocation;
- a generic multi-account strategy framework;
- numeric anti-degeneracy limits and catastrophic recent-performance veto,
  which require the first real diagnostic distribution and a strategy version
  update.
