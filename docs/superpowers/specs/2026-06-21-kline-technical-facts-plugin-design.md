# Kline Technical Facts Plugin Design

## Goal

Add the first real implementation of the dashboard `趋势 / K 线` plugin.

The plugin shows objective technical-analysis facts extracted from the latest
TradingAgents technical report. It does not generate trading advice, change
trade actions, place orders, send notifications, or compute new market
indicators from live quotes.

## User Experience

In the symbol detail decision page, replace the current `趋势 / K 线` placeholder
with a read-only plugin card.

The card shows:

- extraction/cache status
- technical indicator timeframe, such as `日线`
- market data cutoff date, such as `数据截至 2026-06-18`
- report run date
- a compact factual summary
- key fields such as current price, RSI, MACD crossover, ATR, and moving-average
  structure when available

The detail expansion shows the full extracted fact set grouped by timeframe. If
TradingAgents ever reports multiple timeframes, such as daily and weekly
signals, the UI must show them separately and must not combine them into one
conclusion.

If a report mentions an indicator but does not state its timeframe, the UI
shows `指标周期缺失，需复核` and does not interpret that signal as bullish,
bearish, strong, weak, or actionable.

## Data Source

The first version reads only the TradingAgents technical report embedded in
`trading_advice.csv`.

For each advice row:

1. Parse `raw_decision` as JSON.
2. Read `state.market_report`.
3. Send only that report text to the LLM extractor.

The extractor must ignore:

- `FINAL TRANSACTION PROPOSAL`
- `BUY`, `SELL`, `HOLD`, `Underweight`, and similar action/rating language
- position sizing
- entry recommendations
- stop-loss instructions when they are presented as actions rather than
  objective levels

The extractor may include objective support/resistance or risk levels when the
report states them as technical facts.

## Cached Artifacts

Technical facts are generated after a TradingAgents run writes
`trading_advice.csv`. The dashboard never calls the LLM extractor directly.

Dated artifacts:

```text
data/runs/<YYYY-MM-DD>/technical_facts.json
data/runs/<YYYY-MM-DD>/<MARKET>/technical_facts.json
```

Latest artifacts:

```text
data/latest/technical_facts.json
data/latest/<MARKET>/technical_facts.json
```

Market-scoped runs write market-scoped files only. Non-market-scoped runs write
the top-level files.

The extraction step follows the repo's existing artifact pattern:

- write dated artifacts first
- promote latest only when the surrounding premarket run promotes latest
- write atomically
- never partially overwrite a previous latest cache on extraction failure

## Cache Identity

Each symbol record stores a hash of the source technical report. The dashboard
must treat a cache row as stale if the hash does not match the latest advice row.

Record shape:

```json
{
  "market": "HK",
  "symbol": "02476",
  "run_date": "2026-06-19",
  "source": "tradingagents_market_report",
  "source_advice_hash": "sha256:...",
  "source_status": "ok",
  "extraction_status": "ok",
  "market_data_as_of": "2026-06-18",
  "extracted_at": "2026-06-21T09:40:00+08:00",
  "freshness": {
    "status": "fresh",
    "message": "日线数据截至 2026-06-18"
  },
  "facts": {}
}
```

If the same market/symbol/source hash already exists in the applicable latest
or run cache, the extractor reuses the cached row instead of calling the LLM
again.

## Fact Schema

The LLM returns strict JSON. Missing facts remain empty strings or empty arrays.
It must not guess values that are absent from the report.

Top-level shape:

```json
{
  "schema_version": "open_trader.technical_facts.v1",
  "status": "present",
  "source_date": "2026-06-19",
  "market_data_as_of": "2026-06-18",
  "symbol": "HK.02476",
  "timeframes": [
    {
      "timeframe": "daily",
      "timeframe_label": "日线",
      "current_price": "411.60",
      "trend_summary": "",
      "moving_averages": {
        "ema_10": "398.15",
        "sma_50": "368.24",
        "sma_200": "",
        "price_vs_ma": "price above 10 EMA and 50 SMA",
        "ma_alignment": "Price > 10 EMA > Bollinger Mid > VWMA > 50 SMA"
      },
      "macd": {
        "macd": "7.94",
        "signal": "7.26",
        "histogram": "+0.69",
        "crossover": "bullish crossover on June 17",
        "divergence": ""
      },
      "rsi": {
        "value": "56.88",
        "zone": "neutral-bullish",
        "interpretation": "not overbought"
      },
      "bollinger": {
        "middle": "399.62",
        "upper": "459.13",
        "lower": "340.11",
        "price_position": "upper half of the band"
      },
      "atr": {
        "value": "33.17",
        "percent_of_price": "8.1%",
        "volatility_level": "extreme"
      },
      "volume": {
        "vwma": "390.71",
        "volume_pattern": "",
        "volume_confirmation": "price above VWMA"
      },
      "support_resistance": {
        "support_levels": ["398.15", "368.24", "340.11"],
        "resistance_levels": ["459.13", "475"]
      },
      "price_action": {
        "recent_low": "338",
        "recent_high": "475",
        "recent_change": "15% recovery from June 8 to June 18",
        "timeline": []
      },
      "risks": [],
      "evidence_quotes": []
    }
  ]
}
```

Allowed timeframe values:

- `daily`
- `weekly`
- `monthly`
- `yearly`
- `intraday`
- `unknown`

If any indicator is extracted with `timeframe=unknown`, the UI must show a
review warning for that indicator.

## Freshness And Date Rules

The UI must distinguish these dates:

- report run date: when TradingAgents generated the report
- market data cutoff date: the market data date used by the technical report
- extraction time: when the cache was generated

The card headline should prefer:

```text
日线数据截至 2026-06-18
```

Fallback states:

- If market data cutoff is missing: `行情日期缺失，报告生成于 <run_date>`
- If timeframe is missing: `指标周期缺失，需复核`
- If source hash does not match latest advice: `缓存已过期，需重新抽取`
- If extraction failed: `抽取失败，需查看日志`

The dashboard must not display stale technical values when the source hash does
not match.

## Backend Integration

Add a focused extraction module. Responsibilities:

- load advice rows
- parse `raw_decision.state.market_report`
- compute source hashes
- reuse valid cached rows
- call the configured LLM for missing or changed rows
- validate the strict JSON response
- write `technical_facts.json`

`run_premarket()` should call the extractor after writing `trading_advice.csv`.
The daily runner then continues with existing trading-plan and trade-action
steps. Extraction failures should be recorded in the technical facts artifact,
but should not block the whole premarket run unless the failure is caused by a
fatal file write problem during latest promotion.

Add an explicit CLI command for manual repair or backfill:

```bash
.venv/bin/python -m open_trader extract-technical-facts \
  --advice data/latest/trading_advice.csv \
  --data-dir data \
  --date 2026-06-19 \
  --update-latest
```

The command is useful when a previous cache is stale or missing.

## Dashboard Integration

Extend `GET /api/dashboard` holdings with:

```json
{
  "technical_facts": {
    "available": true,
    "extraction_status": "ok",
    "freshness": {
      "status": "fresh",
      "message": "日线数据截至 2026-06-18"
    },
    "facts": {}
  }
}
```

The frontend uses this object for the `趋势 / K 线` plugin card. It does not
fetch another endpoint and does not call the LLM.

## Error Handling

The system is conservative:

- invalid JSON from the LLM becomes `extraction_failed`
- missing `market_report` becomes `missing_source`
- missing timeframe becomes `missing_timeframe` warning
- missing market data date becomes `missing_date` warning
- source hash mismatch becomes `stale` and hides old values

Missing numeric fields stay unknown. They must not be rendered as zero.

## Testing Strategy

Backend tests:

- extract facts from a fake advice row with `raw_decision.state.market_report`
- ignore transaction proposal text
- require timeframe for indicator interpretation
- preserve missing values as unknown
- reuse cache when source hash matches
- mark cache stale when source hash changes
- write dated and latest artifacts atomically
- respect market-scoped output paths

Dashboard tests:

- merge cached technical facts into matching holdings
- show unavailable state when cache is missing
- show stale state and hide old values on hash mismatch
- render Chinese labels for cache, date, and timeframe states

CLI tests:

- manual extraction writes expected artifact paths
- dry run or no-update mode does not promote latest
- invalid advice rows produce clear row-level errors without raw tracebacks

No automated test should call the real LLM, real TradingAgents, or Futu OpenD.
Use fake extractor clients and fixture advice rows.
