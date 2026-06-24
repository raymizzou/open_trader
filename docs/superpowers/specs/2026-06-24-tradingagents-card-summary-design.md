# TradingAgents Card Summary Design

## Goal

Make the `TradingAgents` plugin card answer two trust questions without
expanding the page:

- What is the TradingAgents view and why?
- Which date is the TradingAgents report from, and which date is current
  latest?

The UI must remain inside the existing `TradingAgents` card. Do not add a page
level source panel, historical run list, date picker, source status badge, or
expanded explanation area.

## UI Contract

The `TradingAgents` card displays exactly these five fields, in this order:

1. `TA 观点`
2. `当前动作`
3. `核心理由`
4. `TA 报告日期`
5. `当前 latest`

No other fields are shown in the card by default. In particular, the card must
not show:

- `来源状态`
- historical run lists
- artifact paths
- raw English reports
- expanded reason fields
- explanatory helper text outside the five fields

If a field is unavailable, show `缺失`. Do not omit the row.

## Data Artifact

Add a focused TradingAgents summary artifact instead of enlarging the dashboard
rendering logic or calling an LLM from the browser:

```text
data/runs/<YYYY-MM-DD>/<MARKET>/tradingagents_summary.json
data/latest/<MARKET>/tradingagents_summary.json
```

The artifact is generated after TradingAgents advice, trading plan, and trade
actions are available for a market run. It is promoted to `latest` with the
same market-scoped latest promotion behavior as the existing daily pipeline.

Top-level shape:

```json
{
  "schema_version": "open_trader.tradingagents_summary.v1",
  "generated_at": "2026-06-23T18:37:04+08:00",
  "latest_run_date": "2026-06-23",
  "market": "US",
  "records": []
}
```

Record shape:

```json
{
  "schema_version": "open_trader.tradingagents_summary.v1",
  "market": "US",
  "symbol": "DRAM",
  "latest_run_date": "2026-06-23",
  "ta_report_date": "2026-06-22",
  "ta_view": "低配",
  "current_action": "减仓",
  "core_reason": "内存超级周期仍在，但价格极度延伸、MACD 背离且美光财报前情绪拥挤，所以 TA 建议降低仓位而非清仓。",
  "reason_fields": {
    "main_judgment": "结构性主题仍成立，但短期风险回报转差",
    "evidence_1": "价格远高于均线并出现 MACD 背离",
    "evidence_2": "美光财报前情绪拥挤，失望风险放大",
    "risk_or_counterpoint": "AI 内存超级周期仍支撑保留部分仓位",
    "action_logic": "减仓锁定收益，而不是完全清仓"
  },
  "source_hash": "sha256:...",
  "error": ""
}
```

Dashboard UI reads only these record fields:

- `ta_view`
- `current_action`
- `core_reason`
- `ta_report_date`
- `latest_run_date`

`reason_fields`, `source_hash`, and `error` are for validation and debugging
only. They are not displayed in the card by default.

## Field Semantics

`TA 观点` is the normalized Chinese view from the TradingAgents advice rating,
such as `低配`, `超配`, `持有`, or `卖出`.

`当前动作` is the normalized Chinese action from the current trade action or
premarket action, such as `减仓`, `加仓`, `买入`, `持有`, or `人工复核`.

`核心理由` is a one-sentence Chinese summary of why TradingAgents reached the
view. It must summarize the investment judgment, not merely the current price
trigger. For example, `达到第一目标价` is not a valid standalone core reason.

`TA 报告日期` is the actual report date behind the TradingAgents view. If the
latest run used a fallback report, this field uses `fallback_from_date`.
Otherwise it uses the advice `run_date`.

`当前 latest` is the run date for the currently promoted latest market artifact.

## LLM Extraction

Use a strict JSON LLM extraction step for `core_reason` and `reason_fields`.
The prompt receives:

- full `advice_summary`
- `raw_decision.state.final_trade_decision` when available
- `advice_action`
- `latest_run_date`
- resolved `ta_report_date`
- current normalized action

The prompt must instruct the model to:

- output only JSON matching the fixed schema
- write all display values in Chinese
- keep `core_reason` to one sentence, about 80 to 120 Chinese characters
- explain why TradingAgents reached the view, not why a quote trigger fired
- include main judgment, up to two key evidence points, main risk or
  counterpoint, and action logic
- use `缺失` for unsupported fields
- avoid raw English report prose
- avoid executable order instructions, broker instructions, or detailed sizing

Validation rejects records that:

- omit any required field
- use non-string display fields
- produce blank display fields
- produce English-only display values
- make `core_reason` only a price trigger such as target hit or stop hit
- add unexpected display fields

If LLM extraction fails for a symbol, create a record with all display fields
present. `ta_view`, `current_action`, `ta_report_date`, and `latest_run_date`
come from deterministic sources where possible. `core_reason` falls back to the
existing keyword-based TradingAgents reason helper. If that also fails, use
`缺失`.

## Dashboard Integration

The dashboard loads `data/latest/<MARKET>/tradingagents_summary.json` for each
market present in the portfolio and attaches the matching record by market and
symbol.

The `TradingAgents` plugin card is changed to render the five fixed UI fields.
It no longer derives the card reason from action trigger text. Price trigger
text remains available elsewhere in trade action views but does not populate
the card's `核心理由`.

If no summary record exists for a holding, the card still renders all five rows
with deterministic values where available and `缺失` otherwise.

## CLI And Pipeline

Add a manual extraction CLI:

```bash
.venv/bin/python -m open_trader extract-tradingagents-summary \
  --advice data/latest/US/trading_advice.csv \
  --plan data/latest/US/trading_plan.csv \
  --actions data/latest/US/trade_actions.csv \
  --data-dir data \
  --date 2026-06-23 \
  --market US \
  --update-latest
```

The daily premarket pipeline generates this artifact after trade actions are
available and before market latest promotion completes. Automated tests must use
fake LLM clients; no normal test calls a live LLM.

## Testing Strategy

Unit tests cover:

- report date selection uses `fallback_from_date` before advice `run_date`
- latest run date is copied from the selected market run
- fixed schema validation rejects missing fields and English-only values
- `core_reason` cannot be only a price trigger reason
- failed LLM extraction preserves all five display fields
- dated and latest artifacts are written atomically

Dashboard tests cover:

- records attach to holdings by market and symbol
- the `TradingAgents` card renders exactly the five required labels
- no `来源状态`, history list, artifact path, or reason_fields are rendered
- missing records render rows with `缺失` rather than omitting labels

Pipeline and CLI tests cover:

- manual command writes dated artifacts
- `--update-latest` promotes the market-scoped latest artifact
- daily premarket status artifacts include the new summary path
- fake LLM extraction is used in tests
