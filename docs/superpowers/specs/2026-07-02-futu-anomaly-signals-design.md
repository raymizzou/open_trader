# Futu Anomaly Signals Design

## Purpose

Add a template-driven market signal module to the existing trading decision
dashboard. The module gives each holding independent Futu anomaly evidence for
technical, capital-flow, and derivatives signals across both US and Hong Kong
markets.

The goal is signal coverage, not automatic trading. TradingAgents remains the
primary source of the proposed action. Futu anomaly signals support, challenge,
or constrain that action in a stable, auditable format.

## Scope

Build one aggregated plugin named `市场信号 · 富途异动信号`.

It contains three fixed submodules:

- `technical_anomaly`: K-line patterns and indicator anomalies.
- `capital_anomaly`: funds distribution, broker flow, capital flow, and short
  selling anomalies.
- `derivatives_anomaly`: options, IV, option sentiment, and for Hong Kong stocks,
  warrant and callable bull/bear contract anomalies.

Both `US` and `HK` holdings are included in the first version. Market-specific
classes that do not apply must be shown as `不适用`, not omitted from the user
experience unless the spec explicitly marks that class as market-hidden.

## Non-Goals

- Do not place orders or submit broker trades.
- Do not change `generate-trade-actions` output in the first implementation.
- Do not replace existing `趋势 / K 线`, `新闻 / 舆论`, `TradingAgents`,
  `组合风险`, `公司行动`, or `财报` plugin areas.
- Do not show raw English enum values in the UI, reports, or notifications.
- Do not invent scores, target prices, thresholds, or causal explanations beyond
  the returned Futu anomaly evidence.

## Design Approach

Use a single aggregated signal plugin rather than three separate plugin cards.
This avoids crowding the current plugin grid and gives the user one place to
answer: "Do independent market signals support or challenge this action?"

The visible card has a fixed feel:

1. Header with module availability and template version.
2. Overall signal summary.
3. Three submodule cards: technical, capital, derivatives.
4. Fixed metric cells per submodule.
5. Fixed category rows per submodule.
6. A short template note explaining that missing, no-anomaly, not-applicable,
   and error states are explicit.

Mockups:

- `docs/superpowers/mockups/futu-anomaly-signals-card-mock.html`
- `docs/superpowers/mockups/futu-anomaly-signals-card-mock-desktop.png`
- `docs/superpowers/mockups/futu-anomaly-signals-card-mock-mobile-full.png`

## Data Contract

Extend the existing `open_trader.futu_skill_facts.v1` artifact with signal
modules. The exact Python structures can be refined during implementation, but
the persisted JSON must keep this shape:

```json
{
  "schema_version": "open_trader.futu_skill_facts.v1",
  "run_date": "2026-07-02",
  "market": "US",
  "symbol": "NVDA",
  "name": "NVIDIA",
  "technical_anomaly": {
    "status": "ok",
    "signal": "supportive",
    "confidence": "medium",
    "suggested_constraint": "",
    "window_days": 7,
    "summary": "技术信号支持趋势，但不构成单独买入理由。",
    "categories": []
  },
  "capital_anomaly": {
    "status": "ok",
    "signal": "mixed",
    "confidence": "medium",
    "suggested_constraint": "no_add",
    "window_days": 7,
    "summary": "资金流向与加仓动作存在分歧。",
    "categories": []
  },
  "derivatives_anomaly": {
    "status": "partial",
    "signal": "risk_up",
    "confidence": "low",
    "suggested_constraint": "no_add",
    "window_days": 7,
    "summary": "期权波动率偏高，不宜追高。",
    "categories": []
  },
  "error": ""
}
```

Allowed machine enums:

- `status`: `ok`, `partial`, `missing`, `error`, `stale`
- `signal`: `supportive`, `opposing`, `neutral`, `risk_up`, `mixed`
- `confidence`: `high`, `medium`, `low`
- `suggested_constraint`: empty string, `review`, `reduce_only`,
  `wait_for_event`, `no_add`
- category `state`: `anomaly`, `none`, `not_applicable`, `error`
- category `direction`: `bullish`, `bearish`, `neutral`, `risk_up`, `mixed`,
  empty string

The UI must translate these enums into Chinese:

- `supportive`: `支持`
- `opposing`: `反对`
- `neutral`: `中性`
- `risk_up`: `风险上升`
- `mixed`: `分歧`
- `no_add`: `不加仓`
- `review`: `需复核`
- `reduce_only`: `只减不加`
- `wait_for_event`: `等待事件`
- `ok`: `正常`
- `partial`: `部分可用`
- `missing`: `缺失`
- `error`: `错误`
- `stale`: `已过期`
- `anomaly`: `异常`
- `none`: `无异常`
- `not_applicable`: `不适用`
- `bullish`: `偏多`
- `bearish`: `偏空`
- `high`: `高`
- `medium`: `中等`
- `low`: `低`

The UI may keep stock tickers and standard indicator names such as `NVDA`,
`MACD`, `RSI`, and `IV` because those are domain identifiers, not untranslated
system prose.

## Category Templates

### Technical Anomaly

Fixed category order:

1. `K线形态`
2. `MACD`
3. `RSI`
4. `CCI`
5. `KDJ`
6. `BIAS`
7. `ARBR`
8. `VR`
9. `PSY`
10. `OSC`
11. `WMSR`
12. `BOLL`
13. `MA`

The compact card may show the top three most relevant categories. The detail
view or raw facts artifact must retain every category returned or explicitly
marked as no anomaly.

### Capital Anomaly

Fixed category order:

1. `资金分布与买卖经纪商`
2. `资金流向`
3. `卖空情况`

Each category must preserve dates, direction, amounts, ratios, broker names, and
returned interpretation when available.

### Derivatives Anomaly

Fixed category order for Hong Kong stocks:

1. `牛熊证街货比例`
2. `牛熊证街货价格区间`
3. `期权大单`
4. `期权波动率`
5. `期权量价`
6. `期权情绪`
7. `期权综合信号`

Fixed category order for US stocks:

1. `期权大单`
2. `期权波动率`
3. `期权量价`
4. `期权情绪`
5. `期权综合信号`

US stocks should not show Hong Kong warrant categories in the final compact UI.
If a generic renderer needs a placeholder, show `不适用` in Chinese, never a raw
machine enum.

## Data Flow

1. The CLI reads the latest or explicitly selected portfolio symbols for `US`
   and `HK`.
2. For each symbol, normalize to Futu market-prefixed symbols such as `US.NVDA`
   or `HK.00700`.
3. Fetch anomaly data through the local Futu anomaly script paths used by these
   skills:
   - `futu-technical-anomaly`
   - `futu-capital-anomaly`
   - `futu-derivatives-anomaly`
4. Normalize each skill result into the shared signal module schema.
5. Write dated artifacts under `data/runs/<YYYY-MM-DD>/<MARKET>/`.
6. Promote to `data/latest/<MARKET>/` only when `--update-latest` is explicitly
   used.
7. Dashboard payload assembly loads the latest market-scoped facts and attaches
   the normalized signal modules to each holding.
8. Dashboard JavaScript renders the aggregated `市场信号 · 富途异动信号` card.

## Coexistence With Existing Plugins

The signal module is one aggregated plugin section. It does not replace existing
cards:

- `趋势 / K 线` remains the TradingAgents and local technical-facts conclusion.
- `新闻 / 舆论` remains news, stock digest, and community discussion evidence.
- `市场信号 · 富途异动信号` becomes independent Futu anomaly evidence.
- `组合风险` remains the future portfolio and cash risk gate.
- `公司行动` and `财报` remain future event-blocking modules.

In the existing `renderTradingDecisionPlugins()` layout, this should be inserted
as a full-width or wide card before lower-priority placeholder modules. It should
not be represented as three independent cards in the main grid.

## Error Handling

Missing data must stay explicit:

- Missing skill output -> module `status=missing`, visible `缺失`.
- Futu permission or market data failure -> module `status=error`, visible
  `错误`, with a short Chinese reason.
- Some classes unavailable but others usable -> module `status=partial`, visible
  `部分可用`.
- No anomaly in a class -> category `state=none`, visible `无异常`.
- Market-specific non-applicability -> category `state=not_applicable`, visible
  `不适用` if shown.

Do not convert missing, error, or stale values into neutral signals. Neutral is a
real signal state; unavailable is a data-quality state.

## Testing

Focused tests should cover:

- Schema validation for all new modules and category rows.
- US and HK path generation and latest promotion behavior.
- Normalization from fake technical, capital, and derivatives extractor output.
- Explicit `missing`, `partial`, `error`, `none`, and `not_applicable` states.
- UI translation so raw enum values do not appear in rendered HTML.
- Dashboard payload attachment for both `US` and `HK` holdings.
- Responsive static rendering checks for the aggregated signal card.

Manual verification should include:

- A desktop screenshot matching the mock density.
- A mobile screenshot confirming one-column layout remains readable.
- At least one fake or real US symbol and one fake or real HK symbol in the
  generated artifact.

## Open Decisions Closed In This Spec

- Both US and HK markets are in scope from the first implementation.
- The module is aggregated into one plugin, not three separate main-grid cards.
- User-facing display is Chinese only, except standard tickers and indicator
  identifiers.
- The first implementation does not modify trade-action generation.
