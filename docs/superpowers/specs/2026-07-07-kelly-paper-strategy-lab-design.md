# Kelly Paper Strategy Lab Design

## Goal

Build a Kelly research workflow for stock strategies without touching live
orders. The first version uses Futu simulated trading to collect real order and
fill data for fixed strategy experiments. Kelly sizing is displayed only after
enough completed samples exist; the initial automated trading uses fixed
experiment budgets.

The system must:

- run only against Futu `SIMULATE` trading environment
- bind every simulated order to a strategy template and locked experiment
- collect completed samples from simulated fills and exits
- show a per-holding `凯利` detail entry similar to the existing `做T` button
- provide a strategy experiment page for reviewing locked experiments and
  sample progress

It must not place real-money orders in this version.

## Product Shape

There are three concepts.

### Strategy Template

A strategy template defines reusable trading logic.

Required fields:

- `strategy_id`
- `strategy_name`
- `strategy_version`
- `entry_rule_description`
- `exit_rule_description`
- `max_holding_days`
- `order_type`
- `market_session`

Initial templates:

- `trend_pullback_20d.v1`
- `breakout_10d.v1`

Templates are code/config backed in the MVP. The UI does not create new
strategy templates.

### Strategy Experiment

A strategy experiment is a locked run of one strategy template over a fixed
participant set.

Required fields:

- `experiment_id`
- `experiment_name`
- `strategy_id`
- `strategy_version`
- `start_date`
- `paper_account`
- `experiment_budget`
- `budget_currency`
- `capital_utilization_pct`
- `allocation_mode`
- `max_open_position_per_symbol`
- `status`

Allowed experiment statuses:

- `draft`
- `running`
- `paused`
- `completed`
- `failed`

Once an experiment starts, these fields are locked:

- participant list
- strategy version
- entry and exit rules
- experiment budget
- capital utilization
- allocation mode
- per-symbol position limits

Changing any locked field requires creating a new experiment. This prevents
sample contamination.

### Trade Sample

A trade sample is generated from the lifecycle of a simulated order:

```text
signal -> simulated entry order -> fill -> exit order -> fill -> sample
```

Samples store:

- `sample_id`
- `experiment_id`
- `strategy_id`
- `strategy_version`
- `market`
- `symbol`
- `entry_order_id`
- `exit_order_id`
- `entry_at`
- `exit_at`
- `entry_price`
- `exit_price`
- `quantity`
- `fees`
- `pnl_pct`
- `holding_days`
- `status`

Only completed samples are used for Kelly statistics.

## Experiment Capital Allocation

The MVP uses fixed-budget allocation. Kelly does not size orders yet.

Per-symbol budget:

```text
per_symbol_budget =
  experiment_budget * capital_utilization_pct / locked_participant_count
```

For each entry signal, order notional is:

```text
min(
  per_symbol_budget,
  remaining_experiment_budget,
  remaining_symbol_budget,
  Futu simulated available cash
)
```

If the experiment budget currency differs from the symbol trading currency, the
runner converts the per-symbol budget into trading currency before quantity
calculation. The conversion must use the same FX provider pattern as the rest of
the project and must store both:

- budget currency notional
- trading currency notional

Sample PnL is stored in both trading currency and budget currency when FX data
is available. If FX data is missing, the runner must block new orders for that
symbol rather than guessing.

The first version enforces:

- one open sample per `experiment_id + symbol`
- one strategy version per experiment
- no real-money trading environment
- regular session only for US simulated orders

## User Experience

### Strategy Experiment Page

The first version shows locked experiments rather than an always-editable
checkbox table.

Each experiment page shows:

- Futu simulated connection status
- latest order sync time and status
- strategy template summary
- experiment budget and allocation settings
- locked participant list
- completed sample count
- open sample count
- observed win rate
- sample stage

Sample stages:

- `insufficient`: fewer than 30 completed samples
- `observing`: 30 to 99 completed samples
- `usable_conservative`: 100 to 199 completed samples
- `usable`: 200 or more completed samples
- `paused`: experiment is paused or blocked

Participants are selected before experiment start. After start, the list is
read-only. A new participant set requires a new experiment.

### Holding Detail Entry

The holdings table gains a third action button next to existing entries:

```text
交易决策 | 做T | 凯利
```

Clicking `凯利` opens an inline detail row like the existing `做T` detail mode.

The Kelly detail shows:

- applicable experiments for this symbol
- sample count by experiment
- current sample stage
- observed win rate
- average win
- average loss
- conservative win rate when available
- full Kelly and fractional Kelly only when sample stage permits it

When samples are insufficient, the page must say so directly and avoid showing
precise Kelly sizing.

## Futu Simulated Trading

The automated trading adapter must use only Futu simulated trading:

- `TrdEnv.SIMULATE`
- no real unlock flow
- no real trading password
- no live order placement path

The adapter provides:

- place simulated entry order
- place simulated exit order
- query simulated order list
- query simulated deal/fill list
- sync latest order state into local artifacts

The local system still stores the intent mapping:

- `signal_id`
- `experiment_id`
- `strategy_id`
- `strategy_version`
- `market`
- `symbol`
- intended side
- intended quantity/notional
- stop and target metadata
- Futu order ids once known

Futu remains the source of truth for order and fill fields.

## Data Artifacts

MVP artifacts:

```text
data/latest/kelly_strategy_templates.json
data/latest/kelly_experiment_drafts.json
data/latest/kelly_experiments.json
data/latest/kelly_trade_intents.json
data/latest/kelly_paper_orders.json
data/latest/kelly_samples.json
data/latest/kelly_stats.json
```

Market-scoped files may be added later if needed. The first version can keep
Kelly lab data top-level because experiments may span markets.

Writes must follow existing project artifact rules:

- write dated artifacts first when run-based
- promote `latest` only after a successful run
- write atomically
- do not partially overwrite latest artifacts on sync failure

## Runner Workflow

The paper strategy runner runs on a schedule.

For each active experiment:

1. load the locked experiment and participants
2. verify Futu simulated trading connection
3. sync existing simulated orders and fills
4. close samples whose exit rule has triggered
5. scan locked participants for entry signals
6. apply hard gates
7. place simulated entry orders
8. persist intent and order state
9. regenerate Kelly samples and stats

Hard gates:

- Futu environment must be `SIMULATE`
- experiment must be active
- participant must be locked in the experiment
- current market session must be allowed by the template
- no open sample exists for the same experiment and symbol
- per-symbol and experiment budgets must have remaining capacity
- simulated available cash must be sufficient

## Kelly Statistics

Stats are grouped by both experiment and strategy:

- experiment-level stats answer whether one locked run worked
- strategy-level stats aggregate across compatible experiments of the same
  `strategy_id + strategy_version`

MVP stats:

- completed sample count
- open sample count
- observed win rate
- average win percentage
- average loss percentage
- conservative win rate
- full Kelly
- fractional Kelly
- sample stage

Kelly output rules:

- fewer than 30 completed samples: no Kelly sizing
- 30 to 99 samples: display stats as observation only
- 100 to 199 samples: allow conservative display with 1/4 Kelly
- 200 or more samples: allow 1/2 Kelly display

The MVP does not use Kelly to size simulated orders.

## Error Handling

Connection and execution errors must be explicit:

- Futu OpenD unreachable
- simulated account unavailable
- order rejected
- fill sync failed
- stale order state
- artifact write failed

If order sync fails, the runner must not place new orders for that run. It should
preserve existing artifacts and show the failed sync state in the experiment
page.

## Testing

Every implementation phase must ship with automated tests and a Playwright
verification path. A phase is not complete if only backend tests pass and the
dashboard path is unverified.

Unit tests must cover the backend behavior introduced in that phase:

- experiment lock validation
- capital allocation calculation
- hard gate decisions
- intent to order mapping
- completed sample generation
- Kelly stats thresholds
- dashboard detail mode routing for `kelly`

Integration-style tests must use fake Futu clients for:

- simulated account connection
- order placement
- order query
- fill query
- rejected order handling

No automated test should require a live Futu OpenD connection.

Playwright validation is required for each UI-affecting phase:

- Phase 1: experiment page renders strategy templates, locked experiments, and
  participant lists from local fixtures; holdings table shows the `凯利` button;
  clicking it opens the inline Kelly detail row.
- Phase 2: experiment page shows simulated order sync status, including success
  and failure fixture states.
- Phase 3: manual simulated-order action path shows intent creation, pending
  order state, and synced order id using fake server data.
- Phase 4: runner output fixtures show active/open/completed samples in the
  experiment page and Kelly holding detail.
- Phase 5: Kelly stats render sample stages, observed win rate, average win,
  average loss, conservative win rate, full Kelly, and fractional Kelly only
  when thresholds allow them.

Each phase plan must list exact verification commands, including:

- focused `pytest` commands for the new backend tests
- the dashboard fixture/server command used for UI validation
- the focused Playwright command or script
- expected pass/fail evidence

## Non-Goals

The MVP does not include:

- real-money order placement
- UI creation of arbitrary strategy templates
- editing participants after experiment start
- Kelly-based automatic order sizing
- factor correlation adjustments
- per-symbol historical Kelly estimation
- manual trade import for non-Futu brokers
