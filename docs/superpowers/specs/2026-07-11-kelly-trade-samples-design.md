# Kelly Trade Samples Design

## Context

Kelly strategy experiments already have fixed participants, single-market strategy
budgets, Futu paper order sync, order execution artifacts, and a dashboard section
that renders parameter derivation from `experiment.stats`.

The next stage is to stop treating those stats as static mock data. The system
should derive completed trade samples and Kelly parameters from synced Futu paper
orders, while keeping the original order artifact as the source of truth.

## Goals

- Build completed and open Kelly trade samples from `data/latest/kelly_paper_orders.json`.
- Keep raw synced orders immutable and auditable.
- Generate per-experiment sample statistics used by the existing Kelly Lab UI.
- Make skipped or unsupported order patterns visible instead of silently mixing
  them into the stats.
- Keep the first version constrained to the current rule:
  `max_open_position_per_symbol = 1`.

## Non-Goals

- No FIFO lot engine in this stage.
- No partial fill, split exit, or repeated entry support in this stage.
- No real-money execution changes in this stage.
- No rewrite of strategy definitions in `kelly_experiments.json`.

## Data Flow

The production flow is:

1. Futu paper order sync writes `data/latest/kelly_paper_orders.json`.
2. A new trade sample build command reads synced paper orders and current Kelly
   experiments.
3. The command writes `data/latest/kelly_trade_samples.json`.
4. `load_kelly_lab_state()` loads trade sample stats when present.
5. The dashboard renders the existing strategy tab and parameter derivation from
   the updated `experiment.stats`.

`kelly_experiments.json` remains the strategy definition and seed/mock fixture
source. When `kelly_trade_samples.json` exists, its stats take precedence over
mock stats in the Kelly Lab state.

## Artifact Schema

Create `data/latest/kelly_trade_samples.json` with schema version
`open_trader.kelly_trade_samples.v1`.

Top-level fields:

- `schema_version`
- `generated_at`
- `source_orders_synced_at`
- `sample_count`
- `open_position_count`
- `skipped_order_count`
- `samples`
- `open_positions`
- `stats_by_experiment`
- `diagnostics`

Each completed sample contains:

- `experiment_id`
- `market`
- `symbol`
- `entry_order_id`
- `exit_order_id`
- `entry_submitted_at`
- `exit_submitted_at`
- `entry_price`
- `exit_price`
- `quantity`
- `entry_notional`
- `exit_notional`
- `gross_pnl`
- `net_pnl_pct`
- `result`: `win`, `loss`, or `flat`

Each open position contains the entry-side equivalent fields and the current
unpaired quantity. The first version only records a single open entry per
`experiment_id + market + symbol`.

Diagnostics include skipped orders and skipped groups with explicit reasons,
such as:

- `unsupported_status`
- `partial_fill_not_supported`
- `sell_without_open_entry`
- `repeated_entry_not_supported`
- `exit_quantity_mismatch`
- `missing_price_or_quantity`
- `unknown_experiment`
- `market_mismatch`

## Pairing Rule

Orders are grouped by `experiment_id + market + symbol`, then sorted by
`submitted_at` and stable order id.

The first version accepts only this cycle:

1. One `filled buy` opens a sample.
2. The next `filled sell` for the same group closes it.
3. Sell quantity must equal buy filled quantity.
4. Any unmatched filled buy remains an open position.

Unsupported patterns are excluded from stats and listed in diagnostics:

- submitted, rejected, canceled, pending, or failed orders
- partial fills
- repeated filled buy while a group is already open
- filled sell without an open filled buy
- sell quantity different from entry quantity

This deliberately matches the current strategy constraint that each symbol can
have at most one open position per strategy.

## Statistics

For each experiment, compute:

- `completed_samples`
- `open_samples`
- `winning_samples`
- `losing_samples`
- `flat_samples`
- `observed_win_rate`
- `sample_stage`
- `raw_win_rate`
- `adjusted_win_rate`
- `avg_net_win_pct`
- `avg_net_loss_pct`
- `payoff_ratio`
- `full_kelly_pct`
- `fractional_kelly_pct`
- `suggested_position_pct`
- `sample_adjustment`
- `last_sample_closed_at`
- `last_recomputed_at`
- `parameter_source`
- `skipped_order_count`

Win rate:

```text
raw_win_rate = winning_samples / completed_samples
```

Flat samples count as completed samples but not winning samples.

Average win/loss:

```text
avg_net_win_pct = average(net_pnl_pct where result == win)
avg_net_loss_pct = abs(average(net_pnl_pct where result == loss))
payoff_ratio = avg_net_win_pct / avg_net_loss_pct
```

Kelly:

```text
full_kelly = adjusted_win_rate - (1 - adjusted_win_rate) / payoff_ratio
fractional_kelly = max(0, full_kelly * 0.25)
```

If there are no losses, no wins, or no valid payoff ratio, Kelly outputs remain
blank and `suggested_position_pct` is `0%`.

For small samples, shrink win rate toward 50%:

```text
adjusted_win_rate = 0.5 + (raw_win_rate - 0.5) * min(completed_samples / 200, 1)
```

Sample stage:

- `insufficient`: fewer than 30 completed samples
- `observing`: 30 to 99 completed samples
- `usable_conservative`: 100 to 199 completed samples
- `usable`: at least 200 completed samples

Suggested position is derived from fractional Kelly but remains capped by the
existing strategy and market capital controls. The sample stats do not bypass
order risk checks.

## Kelly Lab Integration

Add an optional loader for `kelly_trade_samples.json`.

When the artifact exists and matches the expected schema:

- index `stats_by_experiment` by `experiment_id`
- attach matching stats to experiments after validation
- preserve existing experiment fields, participants, template, capital, orders,
  and lifecycle state
- add a compact sample summary such as skipped count and source timestamp

When the artifact is missing, the current behavior remains unchanged.

When the artifact exists but is invalid, Kelly Lab should return an unavailable
state with a clear error instead of silently showing stale mock stats.

## Dashboard UI

The existing strategy tab remains the main UI.

The parameter derivation section should show the new fields when present:

- `参数来源：富途模拟盘订单样本`
- `最近更新`
- `最近样本`
- `跳过订单`

No new top-level page is required in this stage. The page should make it clear
that Kelly parameters came from the sample builder, not from static mock stats.

## CLI

Add a Kelly subcommand:

```bash
open-trader kelly build-trade-samples --data-dir data
```

The command reads:

- `data/latest/kelly_experiments.json`
- `data/latest/kelly_paper_orders.json`

It writes:

- `data/latest/kelly_trade_samples.json`

It prints:

- completed sample count
- open position count
- skipped order count
- output path

## Testing

Unit tests:

- build one completed win sample from filled buy and filled sell
- build one completed loss sample
- count unmatched filled buy as open position
- skip submitted, rejected, canceled, and partial orders
- skip sell without open entry
- skip repeated entry while a group is open
- skip exit quantity mismatch
- isolate samples by `experiment_id`, even when symbols overlap
- isolate experiments by market
- compute p, b, adjusted p, Full Kelly, fractional Kelly, and suggested position
- handle zero losses, zero wins, and zero completed samples without division errors

Kelly Lab tests:

- attach trade sample stats when artifact exists
- preserve existing behavior when artifact is missing
- reject invalid schema with a clear error
- keep US and HK stats separate

CLI tests:

- parser accepts `build-trade-samples`
- command wires inputs and writes the expected artifact
- command prints counts and path

Dashboard tests:

- render parameter source from trade samples
- render skipped order count
- preserve existing dashboard when sample artifact is missing

Playwright:

- load the Kelly page fixture with trade sample stats
- verify each strategy tab shows derived sample stats, parameter source, and
  latest update

## Implementation Boundaries

The core sample builder should live in a focused module, separate from order
sync and dashboard rendering. The module should expose pure functions for
building payloads from experiments and paper orders, so unit tests do not need
Futu, files, or the web dashboard.

File writing should be handled by a small wrapper function using the repository's
existing atomic JSON write pattern.
