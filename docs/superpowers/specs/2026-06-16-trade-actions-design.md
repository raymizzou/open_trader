# Trade Actions Design

## Goal

Generate explicit, machine-readable trading action instructions from live Futu
quotes and the structured trading plan.

The first version must tell the user what to do, for which symbol, at what
priority, and at what suggested size. It must also produce stable structured
output that a future automatic order layer can read without parsing prose.

No orders are placed in this phase.

## Inputs

- `data/latest/trading_plan.csv`, or a path passed with `--plan`.
- `data/latest/portfolio.csv`, or a path passed with `--portfolio`.
- Live Futu quote snapshots from Futu OpenD.
- Optional `--date` in `YYYY-MM-DD` format. If omitted, use the latest
  `run_date` present in the active trading plan rows.

Only trading plan rows with `status=active` are eligible for action generation.
All other rows are ignored by the main action loop because they already require
manual plan review.

## Outputs

Add a CLI command named `generate-trade-actions`.

The command writes:

- `data/runs/<YYYY-MM-DD>/trade_actions.csv`
- `data/latest/trade_actions.csv`, unless `--dry-run` is set
- `reports/trade_actions/<YYYY-MM-DD>.md`

The command also prints a concise CLI summary, ordered by action priority, so the
user can immediately see the highest urgency instructions.

All CSV outputs are written atomically. `latest` is updated only after the dated
run output has been written successfully.

## CSV Contract

`trade_actions.csv` uses this fixed field order:

- `run_date`
- `symbol`
- `market`
- `futu_symbol`
- `action`
- `priority`
- `last_price`
- `trigger_status`
- `suggested_quantity`
- `suggested_notional`
- `notional_currency`
- `current_quantity`
- `current_weight`
- `target_max_weight`
- `cash_available`
- `limit_price`
- `stop_price`
- `reason`
- `source_plan`
- `status`
- `error`

`action` is one of:

- `BUY`
- `ADD`
- `TRIM`
- `SELL_STOP`
- `TAKE_PROFIT`
- `HOLD`
- `REVIEW`

`status` is one of:

- `ready`: the row has a concrete action and suggested size.
- `watch`: no trade is currently suggested.
- `review`: the row needs human review before any trade.
- `error`: the row could not be evaluated because required input was invalid.

## Markdown Template

The Markdown report is a human-readable rendering of the same CSV rows. Each
action uses this template:

```text
行动：BUY
标的：US.MSFT
优先级：high
价格：399
建议：买入 3 股，预算约 USD 1,197
条件：当前价格进入 380-400 入场区间
风控：止损 340；目标 450 / 500
原因：来自 2026-06-16 trading_plan.csv，计划仓位上限 12%
状态：ready
```

The Markdown report must not introduce information that is absent from the CSV.
It is a presentation layer over the structured action rows.

## Data Flow

1. Load active rows from `trading_plan.csv`.
2. Load `portfolio.csv` and build a per-symbol position view:
   - current quantity
   - current market value
   - current portfolio weight
   - notional currency
   - same-currency cash balance
   - total portfolio value in HKD
3. Fetch one Futu market snapshot for each active plan symbol.
4. Reuse existing quote evaluation semantics:
   - `stop_loss_hit`
   - `entry_zone`
   - `add_zone`
   - `target_1_hit`
   - `target_2_hit`
   - `watch`
   - `missing_quote`
5. Convert quote status into an action enum.
6. Compute suggested notional and quantity when the action is tradeable.
7. Write CSV and Markdown outputs.
8. Print the CLI summary.

## Action Mapping

- `stop_loss_hit` maps to `SELL_STOP`, priority `critical`, suggested quantity
  equals the current full position.
- `target_2_hit` maps to `TAKE_PROFIT`, priority `high`, suggested quantity
  equals the current full position for the first version.
- `target_1_hit` maps to `TRIM`, priority `medium`, suggested quantity equals
  50% of the current position, rounded down to the nearest whole share.
- `entry_zone` maps to `BUY`, priority `high`.
- `add_zone` maps to `ADD`, priority `medium`.
- `watch` maps to `HOLD`, priority `low`, with no suggested quantity.
- `missing_quote` maps to `REVIEW`, priority `medium`, with no suggested
  quantity.

Any row that lacks the data needed for a tradeable instruction maps to `REVIEW`
instead of producing a partial order-like instruction.

## Sizing Rules

For `BUY` and `ADD`, size is constrained by three values:

1. Trading plan budget.
2. Remaining budget under `target_max_weight`.
3. Available same-currency cash.

Suggested notional is:

```text
min(plan_budget, remaining_target_budget, same_currency_cash)
```

Suggested quantity is:

```text
floor(suggested_notional / last_price)
```

If suggested quantity is less than 1, the row becomes `REVIEW` with an
insufficient-budget reason.

### Plan Budget

The system first tries to parse sizing percentages from `plan_text` or the
structured plan fields:

- Entry actions use the planned entry allocation.
- Add actions use the planned add allocation.

If the plan text does not contain a usable sizing percentage, use conservative
defaults:

- `BUY`: 60% of the target maximum position budget.
- `ADD`: 40% of the target maximum position budget.

### Target Budget

`target_max_weight` is parsed from the trading plan `max_weight` field.

The remaining target budget is:

```text
portfolio_total_value_in_symbol_currency * target_max_weight - current_market_value
```

Symbol-currency portfolio value is derived from `portfolio.csv` by summing all
parseable `market_value_hkd` rows, then dividing by the target symbol row's
`fx_to_hkd`. If the target symbol row lacks `fx_to_hkd`, the row becomes
`REVIEW`.

If `target_max_weight` cannot be parsed, the row becomes `REVIEW`.

### Sell Sizing

For sell-side actions:

- `SELL_STOP`: sell the current full position.
- `TAKE_PROFIT`: sell the current full position.
- `TRIM`: sell 50% of current quantity, rounded down to the nearest whole share.

If there is no current position or the current quantity is not parseable, the row
becomes `REVIEW`.

## Prices

- `last_price` comes from the Futu snapshot.
- `limit_price` is set to `last_price` for `BUY`, `ADD`, `TRIM`, and
  `TAKE_PROFIT` in the first version.
- `stop_price` is set from the trading plan stop loss for `SELL_STOP` and is
  also included for buy-side rows when available.

These fields are order-intent hints only. They are not submitted to a broker.

## Error Handling

Batch-level failures:

- Missing input files.
- Missing required CSV columns.
- Futu OpenD connection failure.
- Invalid CLI arguments.

These fail the command with a clear argparse-style error and do not update
`latest`.

Row-level review states:

- Missing quote.
- Missing position data required for a sell action.
- Missing same-currency cash required for a buy action.
- Unparseable `target_max_weight`.
- Unparseable quantity, weight, market value, cash, or price.
- Suggested quantity below one share.

These produce a `REVIEW` row with `status=review` and a concrete `error` message.
They do not fail the whole batch.

## CLI Summary

The CLI prints rows sorted by priority:

```text
connected to Futu OpenD at 127.0.0.1:11111
loaded 8 active trading plan(s)
actions: 8
ready: 3
review: 2
watch: 3
trade_actions_csv: data/runs/2026-06-16/trade_actions.csv
report: reports/trade_actions/2026-06-16.md
latest: data/latest/trade_actions.csv
critical SELL_STOP US.NVDA qty=10 last_price=120 reason=stop loss hit
high BUY US.MSFT qty=3 notional=1197 last_price=399 reason=entry zone
```

The summary is informational only. The CSV is the machine-readable contract.

## Tests

Add focused tests for:

- Every quote status to action mapping.
- Buy sizing with sufficient cash.
- Buy sizing capped by cash.
- Buy sizing capped by target maximum weight.
- Default 60% entry and 40% add sizing when plan text lacks allocation.
- Budget too small for one share maps to `REVIEW`.
- Stop-loss full-position sell sizing.
- Target-one trim sizing.
- Target-two take-profit sizing.
- Missing quote maps to `REVIEW`.
- Missing cash or missing position data maps to `REVIEW`.
- CLI wiring for `generate-trade-actions`.
- `--dry-run` writes the dated output and report but does not update latest.

Keep `check-futu-plan` as a diagnostic command. It should continue to report
quote-trigger status and must not become responsible for action generation.
