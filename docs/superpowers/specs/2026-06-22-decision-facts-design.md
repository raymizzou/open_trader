# Fixed Decision Facts Design

## Goal

Make the trading decision plugin cards stable and readable by extracting fixed
Chinese fields from existing TradingAgents reports.

The user should see the same fields for every symbol. They should not need to
relearn each card because one symbol exposes RSI/MACD while another exposes a
different set of raw report fragments. Missing values should be shown as
`缺失` only.

## Scope

This design covers only the two plugin modules that have real source data now:

- `趋势 / K 线`
- `新闻 / 舆论`

Other plugin modules remain unchanged. Do not define new templates for company
actions, fundamentals, earnings, market/industry, or portfolio risk in this
work.

This work does not change TradingAgents decisions, trade actions, order
generation, position sizing, notifications, or broker execution.

## Source Data

Use only local TradingAgents output already stored in `trading_advice.csv`.

For each advice row:

1. Parse `raw_decision` as JSON.
2. Read `state.market_report` for `趋势 / K 线`.
3. Read `state.sentiment_report` and `state.news_report` for `新闻 / 舆论`.
4. Compute source hashes from the exact source text used for each module.

No new market, news, social, broker, or web crawler source is introduced by this
design.

## Output Artifact

Add a new fixed-field artifact:

```text
data/runs/<YYYY-MM-DD>/<MARKET>/decision_facts.json
data/latest/<MARKET>/decision_facts.json
```

The dated artifact is written first. Latest promotion follows the existing
market-scoped daily-run promotion behavior. Do not partially overwrite latest on
failure.

Top-level shape:

```json
{
  "schema_version": "open_trader.decision_facts.v1",
  "generated_at": "2026-06-22T21:00:00+08:00",
  "run_date": "2026-06-22",
  "market": "US",
  "records": []
}
```

Record shape:

```json
{
  "market": "US",
  "symbol": "SOXX",
  "run_date": "2026-06-22",
  "source_status": "ok",
  "kline": {
    "status": "ok",
    "source_hash": "sha256:...",
    "fields": {
      "trend": "过热拉升",
      "position": "显著高于 50 日和 200 日均线",
      "momentum": "RSI 处于高位，MACD 偏强",
      "key_levels": "支撑 580，压力缺失",
      "risk": "超买和回撤风险上升"
    }
  },
  "news_sentiment": {
    "status": "ok",
    "source_hash": "sha256:...",
    "fields": {
      "direction": "偏多",
      "change": "较上次由分歧转为偏多",
      "catalyst": "AI 基建和芯片需求预期强化",
      "risk": "估值过高和地缘风险",
      "attention": "关注度升高"
    }
  },
  "error": ""
}
```

## Fixed Fields

### K-Line Fields

The `趋势 / K 线` module must always expose exactly these five display fields:

- `trend` displayed as `趋势`
- `position` displayed as `位置`
- `momentum` displayed as `动能`
- `key_levels` displayed as `关键位`
- `risk` displayed as `风险`

### News And Sentiment Fields

The `新闻 / 舆论` module must always expose exactly these five display fields:

- `direction` displayed as `方向`
- `change` displayed as `变化`
- `catalyst` displayed as `催化`
- `risk` displayed as `风险`
- `attention` displayed as `热度`

Each field must be present for every extracted record. If the source does not
support a field, the extractor writes the literal value `缺失`.

## LLM Extraction Rules

Use a strict JSON LLM extraction step. The prompt must require:

- Output only the `open_trader.decision_facts.v1` record payload.
- Use the fixed fields exactly.
- Write all field values in Chinese.
- Use short field values suitable for dashboard cards.
- Use `缺失` for missing or unsupported evidence.
- Do not include English report prose directly in field values.
- Do not invent facts absent from the source reports.
- Do not output trading recommendations, order instructions, sizing, price
  targets, or automatic execution guidance.

Validation rejects records that:

- omit a fixed field
- add unexpected display fields
- return non-string field values
- return blank values
- contain obvious English-only prose
- include buy/sell/order/sizing instructions outside source-neutral summaries

Invalid extraction for one symbol should not fail the whole run. The affected
module should fall back to all fixed fields set to `缺失` and carry a concise
error string in the record.

## Dashboard Integration

The dashboard should load `data/latest/<MARKET>/decision_facts.json` for markets
present in the portfolio and attach each record to the matching holding.

The `趋势 / K 线` card reads only `decision_facts.kline.fields`.

The `新闻 / 舆论` card reads only `decision_facts.news_sentiment.fields`.

The frontend should render a compact fixed grid:

- field label on top or left
- field value below or right
- `缺失` shown plainly when that field is unavailable

The card must not render raw English values from `technical_facts.json`,
`sentiment_changes.json`, `market_report`, `sentiment_report`, or `news_report`.

If a latest decision-facts record is missing or stale, show the fixed fields with
`缺失` values instead of explanatory placeholder prose.

## Source Freshness

Dashboard freshness checks compare current latest TradingAgents source hashes
against the hashes stored in `decision_facts.json`.

For K-line:

- current hash is computed from `state.market_report`
- stored hash is `kline.source_hash`

For news/sentiment:

- current hash is computed from the combined `state.sentiment_report` and
  `state.news_report`
- stored hash is `news_sentiment.source_hash`

If hashes differ, that module is stale and displays all five fields as `缺失`.

## CLI And Pipeline

Add a manual CLI command:

```bash
.venv/bin/python -m open_trader extract-decision-facts \
  --advice data/latest/US/trading_advice.csv \
  --data-dir data \
  --date 2026-06-22 \
  --market US \
  --update-latest
```

The market-scoped daily premarket run should generate decision facts after
TradingAgents advice is written and before latest promotion.

The extractor should accept fake LLM clients in tests. Normal automated tests
must not call a live LLM.

## Error Handling

- Missing advice file: CLI fails with a clear error.
- Missing `market_report`: K-line fields are all `缺失`.
- Missing both `sentiment_report` and `news_report`: news/sentiment fields are
  all `缺失`.
- LLM invalid JSON: that module falls back to all `缺失`; the symbol run
  continues.
- Validation failure: that module falls back to all `缺失`; the symbol run
  continues.
- Latest promotion failure: preserve the previous latest artifact.

## Testing Strategy

Unit tests:

- source extraction reads the correct TradingAgents report fields
- generated records always contain the fixed K-line field set
- generated records always contain the fixed news/sentiment field set
- missing source reports produce `缺失`
- invalid LLM JSON produces `缺失` for the affected module
- English-only values are rejected
- dated and latest artifacts are written atomically

Dashboard tests:

- dashboard attaches decision facts to holdings by market and symbol
- K-line card renders the five fixed Chinese fields
- news/sentiment card renders the five fixed Chinese fields
- missing or stale record renders `缺失`
- raw English report prose is not rendered in plugin field values

Pipeline tests:

- market-scoped daily run writes `decision_facts.json`
- latest promotion includes `decision_facts.json`
- failed promotion preserves previous latest

Manual verification:

- run the extractor on the current US latest advice
- open the dashboard and inspect `US.SOXX`
- confirm the two plugin cards show fixed Chinese fields
- confirm no English TradingAgents prose appears in those fields

## Acceptance Criteria

- Every displayed `趋势 / K 线` card has exactly `趋势`, `位置`, `动能`, `关键位`,
  and `风险`.
- Every displayed `新闻 / 舆论` card has exactly `方向`, `变化`, `催化`, `风险`,
  and `热度`.
- Missing values display as `缺失`, without extra explanatory filler.
- The dashboard does not show English report prose in those plugin fields.
- Other plugin modules are not changed by this work.
