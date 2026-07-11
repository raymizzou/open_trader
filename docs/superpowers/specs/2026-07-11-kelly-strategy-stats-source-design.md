# Kelly Strategy Stats Single-Source Design

## Goal

Separate trade evidence from derived Kelly parameters and make one artifact the
only runtime source for both dashboard display and automatic order sizing.

The resulting flow is:

```text
kelly_paper_orders.json
  -> kelly_trade_samples.json
  -> kelly_strategy_stats.json
  -> Kelly Lab UI / order intents / risk checks
```

## Current Problem

`kelly_trade_samples.json` currently contains both:

- evidence: completed trade pairs, open positions, skipped-order diagnostics;
- decisions: win rate, payoff ratio, Kelly values, and suggested position.

Consumers are also inconsistent. Kelly Lab overlays the sample-derived stats for
the dashboard, while `build-order-intents` loads the lab with
`include_trade_samples=False` and therefore uses the original stats embedded in
`kelly_experiments.json`. The displayed position can differ from the position
used to build an order.

## Artifact Responsibilities

### `kelly_paper_orders.json`

Raw normalized facts synchronized from Futu SIMULATE accounts. It contains order
identity, experiment attribution, market, symbol, side, timestamps, quantities,
prices, and execution status. It is the source of record for simulated orders.

### `kelly_trade_samples.json`

Audit evidence produced by pairing filled entry and exit orders within one
`experiment_id + market + symbol` stream. It contains:

- `samples`: completed entry/exit pairs and realized result;
- `open_positions`: entries that do not yet have a matching exit;
- `diagnostics`: skipped or unpairable orders;
- source-order synchronization metadata and counts.

It no longer owns `stats_by_experiment` in the target schema. Its purpose is to
answer: "Which trades are the statistical evidence, and how were they paired?"

### `kelly_strategy_stats.json`

Derived, per-experiment strategy statistics produced from the trade-sample
artifact. It contains one record for every configured experiment, including:

- completed and open sample counts;
- winning, losing, and flat sample counts;
- raw and adjusted win rates;
- average win, average loss, and payoff ratio;
- Full Kelly and configured fractional Kelly;
- final `suggested_position_pct`;
- parameter source and calculation timestamps;
- skipped-order count and sample-sufficiency state.

It answers: "What parameters and position should the system use now?"

### `kelly_experiments.json`

Experiment configuration only: strategy reference, market, immutable
participants, budget, account, allocation rules, status, and initial metadata.
Existing embedded `stats` fields may remain during migration for schema
compatibility, but runtime consumers must not use them for display or sizing.

## Production Flow

1. Synchronize Futu SIMULATE orders into `kelly_paper_orders.json`.
2. Build `kelly_trade_samples.json` from those raw orders.
3. Build `kelly_strategy_stats.json` from configured experiments and trade
   samples.
4. Load strategy stats into Kelly Lab for dashboard display.
5. Generate entry intents using the same `suggested_position_pct`.
6. Risk checks validate that percentage, per-symbol budget, market scope, and
   available strategy capital before calculating planned notional.
7. Approved requests are submitted to Futu SIMULATE; later synchronization feeds
   the resulting orders back into the next calculation cycle.

Exit intents do not depend on a positive Kelly percentage because exits reduce
exposure and must remain possible when new entries are blocked.

## Single-Source Rules

- Dashboard and order-intent generation read the same strategy-stats artifact.
- Risk checks consume the percentage copied into the intent and preserve its
  stats generation timestamp/source for auditability.
- Runtime consumers do not independently calculate Kelly values.
- Runtime consumers do not fall back to `kelly_experiments.json` stats.
- A missing, malformed, stale, or experiment-incomplete strategy-stats artifact
  fails closed for new entries and is surfaced clearly in the dashboard.
- The stats builder emits a record for every configured experiment, even when it
  has no completed sample.
- With zero completed samples, the record reports insufficient samples and
  `suggested_position_pct = 0%`; it never substitutes mock win rates.

## Schema and Migration

Introduce `open_trader.kelly_strategy_stats.v1` and a dedicated builder/loader.
The first implementation migrates atomically:

1. Produce and commit a valid `kelly_strategy_stats.json` for current data.
2. Switch Kelly Lab and order-intent generation to that artifact in the same
   release.
3. Keep the current `stats_by_experiment` field in
   `kelly_trade_samples.json` temporarily as compatibility output, but stop all
   runtime reads of that field.
4. After compatibility tests and live verification, remove the duplicate field
   in a later schema revision of the trade-sample artifact.

This staged removal avoids breaking old artifacts while establishing the new
single source immediately at the consumer boundary.

## Error Handling

The builder rejects malformed sample schemas, unknown experiment references, and
non-numeric values required for a calculation. Pairing diagnostics remain in
the sample artifact and are summarized by experiment in strategy stats.

Consumers validate schema version, experiment coverage, percentage format,
source timestamp, and generated timestamp. A validation failure must not produce
an entry order. Dashboard output identifies the artifact and validation problem
instead of silently displaying embedded experiment stats.

Staleness is determined by comparing the stats source-sample timestamp with the
current trade-sample artifact. A mismatch marks stats stale and blocks entries
until stats are rebuilt.

## UI Behavior

The existing parameter-derivation section remains. Its values come exclusively
from `kelly_strategy_stats.json` and show:

- parameter source;
- completed/open samples;
- raw and adjusted win rate;
- payoff inputs and ratio;
- Full and fractional Kelly;
- suggested position;
- latest sample and latest calculation time;
- sample sufficiency, skipped orders, or stale/error status.

No new action button is required. The page displays the latest generated state
directly.

## Testing and Verification

Automated tests cover:

- strategy-stat calculations for wins, losses, flat trades, open positions, and
  zero samples;
- malformed, incomplete, and stale artifacts failing closed;
- Kelly Lab and order intents receiving identical stats and position values;
- exits remaining executable when entry sizing is zero or unavailable;
- risk checks preserving parameter provenance;
- backward-compatible sample output during migration;
- CLI output and atomic artifact writes;
- Playwright rendering of sufficient, insufficient, stale, and invalid states.

Before completion, run the focused pytest suites, the Kelly Playwright suite,
the builders in their real sequence, and compile checks. Restart the dashboard
process so it cannot retain old code, then verify the fresh PID/timestamp, API
payload, rendered stats, and generated order-intent percentage against the same
`kelly_strategy_stats.json` record.

## Out of Scope

- Changing entry, stop-loss, take-profit, trailing-stop, or time-exit rules.
- Using live Futu accounts.
- Mixing markets within one experiment.
- Estimating portfolio-level correlation or portfolio Kelly.
- Removing all legacy experiment/sample fields in the first migration.
