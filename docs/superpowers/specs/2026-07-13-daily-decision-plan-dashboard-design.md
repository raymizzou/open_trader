# Daily Decision Plan Dashboard Design

## Goal

Replace the Dashboard's conclusion-only LLM template with a daily decision
plan that the user can inspect directly. The page must distinguish a validated,
monitorable plan from a non-executable factual fallback and must never present
unvalidated advice as an executable strategy.

This design supersedes the intraday reassessment, broker execution matching,
and `MISSED` behavior in the earlier systematic-plan design for this phase.

## Product Rules

- Each symbol has at most one plan per trading day.
- Ordinary intraday movement never rewrites the plan.
- The next plan is generated on the next trading day from the latest aggregate
  position and market inputs.
- The system does not judge whether the user followed a notification.
- Orders may appear as raw next-day review context, but are not matched to plan
  conditions and do not change an execution status.
- Automatic ordering, broker execution reconciliation, intraday material-event
  intervention, and new-listing trend strategies are out of scope.
- A single-instrument position may not be increased beyond 10% of portfolio
  NAV. Existing overweight positions may be held or reduced, but not increased.

## Two Output Modes

### Validated Plan

A record may use `mode=validated_plan` only when all of the following are true:

- it references a versioned deterministic strategy template;
- all executable numbers have formula and source-fact provenance;
- portfolio risk calculation has completed;
- the referenced backtest result declares the strategy gate passed;
- required market data is current and valid.

The backtest subsystem owns gate policy. This feature consumes its explicit
pass/fail result instead of inventing thresholds in the Dashboard. The plan
displays six-month, one-year, and five-year strategy and benchmark returns when
available, plus maximum drawdown and Sharpe ratio. US strategies identify the
S&P 500 benchmark; HK strategies identify the Hang Seng Index benchmark.

A validated plan contains ordered conditions. Each condition produces an
aggregate target position, not a fixed order quantity. Risk conditions sort
before ordinary price and time conditions.

### Fallback Advice

Use `mode=fallback_advice` when no eligible strategy passes its gate, required
history is insufficient, or a symbol has less than one year of listing history.

Fallback advice contains:

- deterministic market facts such as price distance from moving averages,
  RSI, Bollinger position, and relative volume;
- a concise TradingAgents interpretation;
- a factual recommendation such as observe, do not add, or consider reducing
  risk;
- the 10% portfolio constraint.

Fallback advice has no executable conditions, target order, intraday watcher,
or automatic execution semantics. It is generated once during the daily run.
Missing or corrupt data is not silently converted to fallback advice; the page
shows a generation failure with its cause.

## Daily Artifact

The daily workflow writes:

```text
data/runs/<YYYY-MM-DD>/<MARKET>/decision_plans.json
data/latest/<MARKET>/decision_plans.json
```

The file uses schema version `open_trader.decision_plans.v1` and contains one
record per in-scope symbol:

```json
{
  "schema_version": "open_trader.decision_plan.v1",
  "plan_id": "US.DRAM:2026-07-13:v1",
  "run_date": "2026-07-13",
  "market": "US",
  "symbol": "DRAM",
  "mode": "validated_plan",
  "status": "waiting",
  "current_quantity": "400",
  "current_weight": "0.078",
  "max_weight": "0.10",
  "action_summary": "继续持有，等待条件触发",
  "next_condition_id": "trim-at-resistance",
  "effective_at": "2026-07-13T09:30:00-04:00",
  "expires_at": "2026-07-13T16:00:00-04:00",
  "strategy": {},
  "conditions": [],
  "backtests": [],
  "fallback": null
}
```

Quantities, prices, percentages, and metrics are serialized as decimal strings.
The validated-plan `strategy`, `conditions`, and `backtests` objects carry
stable IDs, formulas, inputs, source dates, benchmark names, and gate results.
The fallback object carries structured fact rows and the recommendation.

Plan writes are atomic: write a sibling temporary file, validate it, then
replace the destination. A failed run must not publish a partial latest file.

## Intraday Edge Triggers

Only validated plans enter the existing market watcher. The watcher evaluates
the active day's conditions against current market data and appends events to:

```text
data/runs/<YYYY-MM-DD>/<MARKET>/plan_events.jsonl
```

A trigger occurs only on a false-to-true transition:

```text
false -> true: append condition_triggered and notify
true  -> true: no event and no notification
true  -> false: append condition_reset
false -> true: append another condition_triggered and notify again
```

The event stream therefore permits multiple occurrences of the same condition
without notification spam while the condition remains true. Each notification
contains market, symbol, trigger fact, suggested action, current aggregate
position, target aggregate position, parameter source, and a deep link to the
plan page.

The v1 event types are `condition_triggered`, `condition_reset`,
`notification_sent`, `notification_failed`, and `plan_expired`. Every event has
a stable event ID, plan ID, condition ID when applicable, occurrence time, and
JSON-compatible payload.

The watcher does not inspect orders, decide whether the user complied, or
regenerate a plan. At session end the plan expires. The next daily workflow
uses the latest aggregate position as the next plan's starting point.

## Dashboard Data Flow

`load_dashboard_state` reads the latest decision-plan records and attaches one
normalized `decision_plan` object to each holding. It also reads today's event
stream and the most recent earlier trading-day plan and events for the same
market and symbol.

The Dashboard API exposes only display-ready structured fields. The browser
does not calculate risk, strategy eligibility, backtest gates, or trigger
state. If a decision-plan file is missing or invalid, the final tab displays a
failed state instead of falling back to the legacy LLM template.

Notification links open the holding detail with the Final Decision tab active.
The URL identifies the market and symbol so refresh and browser back preserve
the selected plan.

## Final Decision Page

The approved Mock establishes the hierarchy.

### Validated Plan Layout

1. Status banner: plan state, current action, current aggregate position,
   nearest condition, target position, 10% risk status, and expiry.
2. Ordered condition list: priority, trigger, suggested action, target
   position, parameter provenance, current state, and today's trigger count.
3. Backtest gate: six-month, one-year, and five-year strategy-versus-benchmark
   returns, maximum drawdown, Sharpe ratio, and visible pass/fail labels.
4. Parameter provenance: formulas, inputs, source dates, and calculated values.
5. Previous trading-day review, collapsed by default.

### Fallback Layout

1. Prominent `非执行型建议` and `禁止加仓` labels.
2. Objective technical-fact cards.
3. Concise TradingAgents risk recommendation.
4. Explanation of why no validated plan exists.
5. The 10% portfolio constraint.
6. Previous trading-day review, collapsed by default.

The review records objective facts only: prior condition occurrences, prior
closing position, current starting position, and raw order context when
available. It contains no compliance score or execution judgment.

## Visual And Interaction Rules

- Reuse the current Dashboard's light surfaces and blue accent rather than
  introducing a new design system or dependency.
- Use text labels with semantic colors; color alone never communicates state.
- Use tabular figures for prices, positions, percentages, and metrics.
- Interactive targets are at least 44 by 44 CSS pixels.
- Keyboard focus remains visible and the plan sections follow heading order.
- Desktop uses a dense two-column layout; widths below 900 pixels stack the
  evidence rail; phone widths use single-column condition cards.
- No horizontal page scrolling at 375 pixels.
- Motion is limited to 150-200ms state transitions and respects
  `prefers-reduced-motion`.
- The previous-review disclosure uses a semantic `details`/`summary` control.

## Error Handling

- Invalid schema, duplicate symbol/day records, non-decimal numeric strings,
  missing provenance, or mismatched plan dates block publication.
- A backtest marked failed can only produce fallback advice.
- Missing current facts produces a visible generation failure, not neutral
  facts and not a stale plan.
- JSONL replay never silently ignores a malformed line; it reports the event
  file as unavailable while leaving the immutable daily plan readable.
- Notification failure appends a failure event and remains retryable on the
  next false-to-true occurrence. It does not change the plan.

## TDD Seams

The user-approved public seams are:

1. Daily generation: given normalized facts, strategy/backtest eligibility,
   and portfolio state, return either a validated plan or fallback advice and
   publish an atomic daily artifact.
2. Edge trigger monitoring: given the plan, prior condition truth state, and a
   market snapshot, append exactly the expected reset/trigger events and emit
   notifications only for false-to-true transitions.
3. Dashboard/API: given plan and event artifacts, expose the normalized holding
   payload and render both approved layouts on desktop and mobile.

Each slice follows red-green TDD through these public boundaries. Tests do not
mock private helpers or reproduce production formulas in assertions.

## Acceptance And Deployment

Every modification must end with `make acceptance`. `FAIL` is fixed and rerun;
`BLOCKED` is reported without substituting fixtures or screenshots. Only
`PASS` permits handoff.

After `PASS`, restart the Dashboard from the exact accepted Git SHA. Verify the
new PID, working directory, SHA, fresh log, and HTTP 200 response, then provide
the direct plan URL for user review.

## Deferred Work

- broker order attribution and execution-status inference;
- automatic simulated or real order submission;
- execution-discipline scoring;
- intraday plan regeneration;
- material-event intervention;
- dedicated trend strategies for symbols with less than one year of history;
- database persistence.
