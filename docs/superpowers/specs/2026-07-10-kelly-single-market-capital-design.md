# Kelly Single-Market Capital Rules Design

## Goal

Make Kelly paper-trading experiments market-scoped and budget-scoped before
expanding automated simulated trading. The system should be simple enough to
operate safely in the first live-simulated phase:

- one experiment runs in exactly one market
- all participants in that experiment belong to that same market
- participants are fixed by local config/data and are not editable in the UI
- the same symbol may appear in multiple experiments
- market-level simulated capital starts from fixed constants

This supersedes earlier mixed-market mock data. A strategy template remains a
reusable rule definition, while a strategy experiment is a locked market-specific
run of that template.

## Rules

### Strategy Template

A strategy template is market-agnostic. It defines entry, stop-loss, take-profit,
trailing-stop, and time-exit rules.

The same template can be instantiated separately for different markets:

```text
trend_pullback_20d + US -> US experiment
trend_pullback_20d + HK -> HK experiment
```

The template does not hold the participant list or capital pool.

### Strategy Experiment

Each experiment must declare a top-level `market`:

```json
{
  "experiment_id": "trend_pullback_20d_us_20260707",
  "strategy_id": "trend_pullback_20d",
  "strategy_version": "v1",
  "market": "US",
  "experiment_budget": "30000",
  "budget_currency": "USD"
}
```

Validation rules:

- `experiment.market` is required.
- `experiment.market` must be one of `US`, `HK`, or `CN`.
- Every `participant.market` must equal `experiment.market`.
- Mixed-market experiments are invalid and should fail loading instead of being
  partially rendered.
- Existing participant fields remain locked after experiment creation.

This means the current mixed mock experiment must be split or cleaned. The
preferred shape is:

```text
trend_pullback_20d_us_mock_20260707:
  market: US
  participants: US.DRAM, US.RAM, US.SOXX
  budget: 30000 USD

trend_pullback_20d_hk_mock_20260707:
  market: HK
  participants: HK.02840
  budget: 200000 HKD
```

The same symbol may appear in multiple experiments if the user intentionally
assigns it to multiple strategies. Attribution remains by `experiment_id`.

### Market Capital Pools

The first version uses fixed per-strategy paper capital constants:

```text
US: 30000 USD
HK: 200000 HKD
CN: 150000 CNY
```

CN is represented in config and validation but not enabled in the UI workflow
yet. CN orders may be read for diagnostics, but no CN strategy experiment is
required in this phase.

The experiment budget should default from the per-strategy market budget:

```text
US experiment -> 30000 USD
HK experiment -> 200000 HKD
CN experiment -> 150000 CNY
```

In this phase, the experiment budget is not user-editable in the UI. If a later
phase needs multiple experiments sharing one account-level pool, that phase must add an
explicit reserved/consumed allocation ledger. The current phase only records the
declared budget per experiment and does not attempt cross-experiment cash
reservation.

## Order Flow Impact

Order intents should carry the experiment market and budget currency from the
experiment. Risk checks should reject an intent when:

- the experiment market is missing
- the intent symbol market does not match the experiment market
- the budget currency does not match the experiment market currency
- the target Futu trading market differs from the experiment market

Execution should still use Futu `SIMULATE` only. The CLI market selector should
match the intended market:

```text
--trd-market US for US experiments
--trd-market HK for HK experiments
```

The first implementation does not automatically net exposure across strategies
that share a symbol. That is a later portfolio-level risk feature.

## UI Impact

The Kelly lab should show market and fixed budget as first-class experiment
metadata:

```text
市场: US
模拟资金池: USD 30000
```

The participant list should be read-only. It should not include controls that
imply the user can add, remove, or switch symbols inside a running experiment.

When multiple market-specific experiments use the same template, the UI should
display them as separate experiments, not as one mixed-market tab. Labels should
make the market visible, for example:

```text
趋势回调 20D / US
趋势回调 20D / HK
```

If invalid mixed-market data is loaded, the UI should not silently hide the bad
participant. The backend loader should reject the payload with a clear error so
the operator fixes the experiment definition.

## Testing

Automated tests should cover:

- experiment loading rejects a participant whose market differs from
  `experiment.market`
- valid US and HK experiments load with their fixed budget/currency
- order intent generation preserves `experiment_market`
- risk checks block cross-market intents
- dashboard rendering shows market and simulated capital pool
- Playwright verifies the Kelly lab displays separate US and HK experiments and
  no editable participant controls

Live Futu verification is only needed when changing execution or sync behavior.
Pure config validation and UI display changes should use unit tests and
Playwright fixtures.
