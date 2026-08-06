# Systematic Trading Plan Design

## Goal

Turn the current conclusion-only Dashboard card into a low-frequency,
systematic investment workflow that says what target position to hold under
each condition, monitors those conditions, observes broker orders and fills,
and automatically produces the next plan.

The system exists to reduce emotional, discretionary decisions. It is not a
high-frequency trading engine. Real accounts remain notification-only and are
executed manually; Futu simulated accounts may execute automatically.

## Current Problem

The repository already has most of the required pieces, but they are not one
closed loop:

- `trading_plan.py` parses TradingAgents prose into fixed plan columns.
- `trade_actions.py` evaluates current quotes and produces a point-in-time
  action.
- `futu_watch.py` polls quotes and emits one-shot alerts.
- the Kelly modules already model order intents, risk checks, simulated order
  execution, and paper-order synchronization.
- `backtest.py` can run a fixed plan through Backtrader or the local simulator.

The Dashboard currently treats the point-in-time trade action as the final
investment conclusion. For DRAM this turns a real conditional plan -- trim on
a bounce toward the 10 EMA, trim on time expiry, and exit at a protection level
-- into `低配，当前动作是观察`. Existing evidence and parameter derivations are
hidden in other tabs, the live price can be mislabeled as the strategy price,
and the final card has no complete path from trigger to target position.

The current backtest also replays today's absolute levels. That can test a
one-shot threshold after a selected date, but cannot establish whether the
rule that generated those levels worked historically.

## Product Positioning

Call this a **systematic investment decision system**, not a high-frequency
quantitative system.

Its normal cadence is:

1. Perform one full evaluation before the market opens each trading day.
2. During the session, monitor only the conditions in the active plan.
3. Do not let ordinary intraday movement rewrite an active plan.
4. Re-evaluate immediately after a completed action or a material invalidation
   event.
5. Re-evaluate missed and expired plans during the next scheduled premarket
   run.

## Authority Split

LLM output is research input, not executable authority.

The LLM may provide:

- the current investment view;
- supporting and opposing evidence;
- material event risks;
- candidate strategy templates and parameters.

Deterministic code owns:

- strategy eligibility;
- formula evaluation;
- condition evaluation;
- target-position calculation;
- portfolio risk adjustment;
- order intent generation;
- plan and order state transitions;
- backtest execution.

An LLM-proposed formula that is not already approved is a candidate rule. It
cannot silently enter an established instrument's active plan.

## Deep Module Seams

Keep three small external interfaces and hide implementation details behind
them.

### Strategy Module

```text
evaluate(strategy, market_state, portfolio_state, plan_state)
  -> proposed target positions and conditions
```

The same interface is called by live monitoring and historical backtests.
Market-state adapters provide either current data or as-of historical data.

### Portfolio Risk Module

```text
apply_risk(portfolio_state, proposed_targets)
  -> final targets, rule results, and derivations
```

The first implementation has one real rule: the 10% single-instrument limit.
Additional portfolio rules remain internal additions to this module and do not
change its interface.

### Execution Module

```text
execute_or_notify(final_target, execution_mode)
  -> order or notification events
```

Use two existing real adapters at this seam:

- real account: notify the user, then observe Futu and Tiger orders;
- Futu simulated account: submit an order and observe its lifecycle.

Do not create a second strategy implementation for backtests or another plan
model for the Dashboard.

## Strategy Templates And Plan Instances

A strategy template is a versioned rule with:

- a stable identifier and version;
- eligibility requirements;
- named formula parameters;
- required input facts and lookback periods;
- deterministic target-position rules;
- deterministic expiry and invalidation rules;
- backtest status and result reference.

A plan instance applies one approved strategy template to one symbol at one
as-of time. It contains:

- plan ID and version;
- market and symbol;
- current aggregate position across all brokers;
- strategy-proposed target;
- risk-adjusted final target;
- ordered conditions;
- execution mode;
- effective and expiry times;
- input fact IDs, source dates, and freshness;
- formulas, inputs, rounding rules, and calculated values;
- supporting evidence, strongest opposing evidence, and invalidation facts;
- strategy-template and backtest-result references.

Plan instances are immutable. A manual change, daily replacement, or automatic
re-evaluation creates a new version and records why the old version ended.

## Target Positions, Not Fixed Orders

The authoritative output is a target aggregate position, not an instruction to
buy or sell a fixed quantity.

```text
order quantity = final target position - current aggregate position
```

This makes retries idempotent and naturally handles manual trades, partial
fills, and holdings split across brokers.

For a 400-share DRAM example, a plan can be represented as:

```text
current state: waiting to reduce

price >= 65
  -> aggregate target position = 268-300 shares

deadline reached without price >= 65
  -> aggregate target position = 268-300 shares

price <= 57 at any stage
  -> aggregate target position = 0 shares

thesis invalidated
  -> stop using the old plan and automatically re-evaluate
```

Conditions at the same stage can be alternatives. When either the price or
time condition reaches the same reduction target, its sibling becomes
irrelevant. Protection and later-stage conditions remain active until the
position is closed or a new plan supersedes them.

## Parameter Provenance

Every executable price, quantity, percentage, and deadline must be traceable to
a current source fact or a reproducible formula. A raw LLM number is invalid.

Example:

```text
rule: support_minus_atr_buffer
version: v1
formula: protection level = support - 0.4 * ATR
inputs: support=59, ATR=5.35
rounding: nearest valid market tick
result: 57
```

Generation follows this order:

1. Build normalized, dated facts.
2. Select an eligible strategy template.
3. Calculate all parameters with deterministic formulas.
4. Validate source IDs, dates, units, formulas, ranges, and market rounding.
5. Retry generation with validation errors when repair is possible.
6. Publish a blocked generation result only after repair fails.

Failure is an exceptional fallback, not the normal source of missing plan
fields.

## Plan Lifecycle

The lifecycle is:

```text
DRAFT -> VALIDATING -> ACTIVE -> TRIGGERED
                           |          |
                           |          +-> order/notification lifecycle
                           |                    |
                           |                    +-> COMPLETED -> REASSESSING
                           |
                           +-> INVALIDATED -> REASSESSING
                           +-> EXPIRED -> next premarket REASSESSING

TRIGGERED without execution by session end
  -> MISSED -> next premarket REASSESSING
```

Rules:

- A validated plan activates automatically; there is no manual approval or
  resume step.
- An invalidated plan automatically enters `REASSESSING`; a valid replacement
  automatically activates and both transitions are notified.
- A normal triggered plan waits for its execution result before generating the
  next plan.
- `MISSED` means the system notified the user but no matching execution
  occurred by session end.
- `EXPIRED` means the plan ran normally but its conditions did not occur before
  expiry.
- These outcomes remain distinct so strategy quality and execution discipline
  can be measured separately.

## Broker Order Observation

Every monitoring cycle evaluates current quotes and refreshes broker order and
position state. Do not wait until the next day to infer execution from a
position CSV.

Futu and Tiger order adapters must expose normalized events for at least:

- submitted;
- partially filled;
- filled;
- canceled;
- rejected;
- expired.

The first version deliberately uses a simple manual-order association rule:

```text
same symbol + expected side + order time after the trigger
```

Aggregate matching orders and fills across Futu and Tiger. The plan is about
the user's aggregate position, not a particular broker account. An opposite
side or otherwise conflicting order is `MANUAL_DIVERGENCE`.

The aggregate broker position remains an invariant check, but broker order and
fill records are the execution-state authority.

## Execution Modes

### Real Accounts

- Activate validated plans automatically.
- Notify when a condition triggers.
- Let the user submit the order manually in any broker.
- Observe matching Futu and Tiger order events.
- Generate the next plan only after the aggregate target is reached, or after
  the plan becomes missed, expired, or invalidated.

### Futu Simulated Accounts

- Use the same strategy, target-position, and risk logic.
- Generate an order intent and run the existing deterministic risk checks.
- Submit automatically through the existing Futu simulated execution adapter.
- Advance the plan from actual order and fill events.

Real-account automatic order submission is out of scope.

## Portfolio Risk V1

Use the latest aggregate portfolio across brokers and convert values to the
portfolio base currency, HKD.

The first active rule is:

```text
single-instrument target value / aggregate portfolio net asset value <= 10%
```

Apply it as a new-risk limit, not a forced rebalance:

- If current weight is at or below 10%, a new target cannot exceed 10%.
- If current weight is already above 10%, holding or reducing is allowed.
- An existing overweight position is not automatically forced down to 10%.
- Any target that increases an already-overweight position is blocked.
- The plan report displays `现有仓位超限，禁止加仓`.
- Portfolio risk may reduce a strategy target but may not turn a reduction into
  an increase.

The module's data and interface must preserve the calculation inputs and rule
results. Sector limits, cash reserves, correlation, VaR, and other policy rules
are deferred until they are actually selected.

## Instrument-Age Strategy Routing

Use exactly two strategy pools in the first version.

### New Instruments

An instrument with less than one year of listing history uses a simple
new-listing strategy based on available liquidity and momentum facts, such as
turnover, volume trend, price momentum, volatility, and tradability.

- Long-history backtest results are not an activation gate.
- The plan is labeled as a new-instrument strategy with insufficient long-term
  history.
- Complex long-lookback rules are ineligible.
- Parameter provenance and the 10% portfolio rule still apply.

This waiver applies only when the instrument is genuinely too new. A failed or
incomplete data download must not be relabeled as a new instrument.

### Established Instruments

An instrument with at least one year of listing history may use only an
eligible, versioned strategy rule that passes the available historical gates.
Rules declare their own minimum warm-up and input requirements, so a rule that
requires more history remains ineligible even when the instrument is one year
old.

## Backtest Contract

Backtests validate the deterministic quantitative rule, not the LLM's
historical judgment accuracy.

The Strategy Module must run unchanged in both environments:

```text
live adapter       -> current market, order, and portfolio events
historical adapter -> as-of bars, simulated fills, and historical portfolio
```

At each historical time, calculate absolute values from facts that were
available then. Formula coefficients, indicator periods, target percentages,
expiry rules, and state transitions are strategy parameters. Today's absolute
prices and quantities are plan-instance results and must not be copied backward
through history.

For example:

```text
historical protection level at t = support(t) - 0.4 * ATR(t)
```

No future bar, later indicator value, current report, or current absolute level
may influence the calculation at `t`.

The current Backtrader adapter, local fee/slippage support, trades artifact,
and equity curve should be reused. Replace the duplicate fixed-threshold
simulation core with the shared Strategy Module rather than adding another
backtest engine.

## Backtest Horizons And Benchmarks

For each eligible horizon, run the strategy and its benchmark over the same
dates and base-currency assumptions:

- six months;
- one year;
- five years.

Benchmarks:

- US instruments: S&P 500 total return;
- HK instruments: Hang Seng Index total return.

Use dividend-adjusted total returns. Convert both strategy and benchmark to HKD
with the same dated FX series. Apply configured commissions and slippage to the
strategy.

Display for both strategy and benchmark:

- total return and the percentage-point difference;
- whether the strategy beat the benchmark;
- maximum drawdown;
- annualized Sharpe ratio;
- actual date range and trading-day count.

Maximum drawdown is the largest peak-to-trough decline in the daily equity
curve.

Calculate annualized Sharpe from daily returns:

```text
mean(daily return - matching daily risk-free return)
---------------------------------------------------- * sqrt(252)
          standard deviation of daily returns
```

Use matching historical risk-free-rate data and disclose its source. If that
series is unavailable, report Sharpe as unavailable rather than silently using
zero.

## Established-Rule Backtest Gate

For an established instrument, a new rule passes only when:

1. Data quality is valid and no look-ahead input is detected.
2. Net of costs, it beats the market benchmark in at least two of the three
   available standard horizons.
3. The longest available standard horizon beats the benchmark.
4. Strategy Sharpe is at least the benchmark Sharpe.
5. Strategy maximum drawdown is no worse than benchmark maximum drawdown.

If only the six-month and one-year horizons are available, both must pass. An
instrument with less than one year of genuine listing history follows the new
instrument path instead of this gate.

Rules and parameters are versioned before evaluation. Any parameter change is
a new rule version; failed attempts remain visible in the event history. Do not
overwrite a failed result with a tuned result under the same identity.

Backtest results are cached by at least:

```text
symbol + strategy version + parameters + market-data end date + data hash
```

Reuse a valid cached result when producing a daily plan and update it as new
market data arrives. An update failure makes the displayed result stale with an
explicit date; it does not rewrite the prior result as current. A new
established-instrument rule cannot activate without a valid passing result.

## Dashboard Final-Decision View

Replace the current generic model template with an execution-plan view.

The first screen shows:

```text
current state
next action
current aggregate position -> final target position
execution mode: real notification / simulated automatic
plan version and effective time
```

The primary table contains:

| Condition | System action | Final target | State | Source and derivation |
|---|---|---:|---|---|

Below it, show:

- strategy target and portfolio-risk-adjusted target;
- supporting evidence and the strongest opposing evidence;
- invalidation conditions;
- explicit parameter formulas, inputs, dates, and rounding;
- backtest rows for six months, one year, and five years;
- strategy and benchmark return, maximum drawdown, and Sharpe;
- backtest data date, costs, FX, risk-free source, and data-quality status;
- plan event history, including manual divergence, missed execution, and
  expiry.

Do not show an LLM confidence percentage or a composite backtest confidence
score. The user sees the actual logic and metrics.

Keep the existing TradingAgents, trend/K-line, news/sentiment, and Futu anomaly
tabs as evidence drill-downs. The final plan references those facts but does
not require the user to assemble the decision manually across tabs.

## Notifications

Notifications are state-change messages, not generic summaries. They include:

- plan ID and version;
- symbol and execution mode;
- condition that changed state;
- current price and current aggregate position;
- final target position and implied side/quantity;
- source rule and key risk reason;
- whether the system is waiting for a manual order, observing an order,
  automatically re-evaluating, or has activated a replacement plan.

An invalidation notification may combine `old plan invalidated` and `new plan
activated` once automatic re-evaluation succeeds. A generation failure reports
the real blocked state and does not require the user to run a manual recovery
command.

## Event History V1

Use an append-only JSONL event history as the first source of plan-lifecycle
truth. Required event families include:

- plan created, validated, activated, superseded, invalidated, expired, missed;
- condition triggered and notification sent;
- order submitted, partially filled, filled, canceled, rejected, expired;
- target reached, manual divergence, and re-evaluation started/completed;
- backtest started, passed, failed, waived, or stale;
- portfolio-risk rule applied or blocked.

Keep existing CSV and JSON `latest` artifacts as derived read views for current
callers and the Dashboard. Do not introduce a database in this version.

The event reader/writer seam must not expose JSONL-specific behavior to the
strategy, risk, execution, backtest, or Dashboard modules. A later database can
replace storage without changing their interfaces.

## Non-Goals

This version does not add:

- a database;
- real-account automatic order submission;
- high-frequency or tick-level strategy recalculation;
- historical replay of TradingAgents or another LLM;
- a composite confidence score;
- parameter optimization;
- sector, cash-reserve, correlation, VaR, or other unselected portfolio rules;
- a new backtest framework;
- a separate Dashboard-only plan model.

## Verification And Acceptance

Implementation is not complete until focused automated tests cover:

- identical strategy behavior from live and historical adapters;
- target-position idempotency and partial fills;
- alternative and sequential condition transitions;
- automatic invalidation and replacement-plan activation;
- real manual-order observation across Futu and Tiger;
- simulated automatic execution through the existing adapter;
- `MISSED`, `EXPIRED`, and `MANUAL_DIVERGENCE` classification;
- parameter provenance and deterministic recalculation;
- 10% risk behavior below and above the current limit;
- new-versus-established instrument routing;
- no-look-ahead backtest evaluation;
- benchmark, FX, costs, maximum drawdown, and Sharpe calculations;
- established-rule pass/fail gates and new-instrument waiver;
- immutable event history and derived latest views;
- final-decision rendering on desktop and mobile.

Run the affected command and workflow directly, inspect broker/watcher process
state and fresh logs, and restart any long-running process still holding old
code.

Because this changes Dashboard behavior, run `make acceptance` as the final
verification step. Only `PASS` may be reported as complete. `FAIL` must be
fixed, and `BLOCKED` must be reported as blocked without substituting mocks,
curl, screenshots, or unit tests.
