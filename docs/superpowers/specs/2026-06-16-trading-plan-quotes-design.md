# Trading Plan Quotes Design

## Goal

Connect live Futu quotes to the trader advice template by creating a structured
`trading_plan.csv` and evaluating each current quote against that plan.

## Data Flow

1. `run-premarket` writes `trading_advice.csv`.
2. `build-trading-plan` reads `trading_advice.csv` and writes:
   - `data/runs/<YYYY-MM-DD>/trading_plan.csv`
   - `data/latest/trading_plan.csv` unless `--dry-run` is set
3. `check-futu-plan` reads `trading_plan.csv`, fetches live Futu quotes, and
   prints the current plan status for each symbol.

## Plan CSV

The plan keeps one row per successful advice row. Columns:

- `run_date`
- `symbol`
- `market`
- `rating`
- `entry_zone_low`
- `entry_zone_high`
- `add_price`
- `stop_loss`
- `target_1`
- `target_2`
- `max_weight`
- `catalyst`
- `time_horizon`
- `plan_text`
- `status`
- `error`

Rows with unstructured or failed advice are preserved with `status=manual_review`
or `status=error` so the pipeline remains auditable.

## Quote Evaluation

`check-futu-plan` maps US symbols to `US.<SYMBOL>` and HK numeric symbols to
`HK.<5-digit symbol>`, fetches one market snapshot, and classifies each row:

- `stop_loss_hit`: last price is at or below stop loss.
- `entry_zone`: last price is between entry-zone low and high.
- `add_zone`: last price is near the add price, within 1%.
- `target_2_hit`: last price is at or above target 2.
- `target_1_hit`: last price is at or above target 1.
- `watch`: no price trigger is active.
- `missing_quote`: Futu did not return a quote.

No orders are placed. The output is diagnostic and decision-support only.
