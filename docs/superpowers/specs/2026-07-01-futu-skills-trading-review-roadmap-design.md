# Futu Skills Trading Review Roadmap

## Purpose

Preserve the long-term product goal for the trading-decision plugin so future
sessions can restart from a clear scope boundary instead of re-opening the whole
discussion.

The goal is to evolve the current dashboard plugin area into a Futu
Skills-driven trading review system. TradingAgents remains the primary source
of the trading thesis and proposed action. Futu Skills provide independent
facts, market signals, and risk constraints that either support, challenge, or
downgrade that action before execution.

This is not a new automatic trading engine. It is an execution review layer.

## Current Boundary

The existing dashboard already has a trading-decision plugin layout. Today:

- `TradingAgents` is the real primary decision source.
- `Trend / K-line` and `News / Sentiment` can read fixed decision facts derived
  from current pipeline artifacts.
- `Corporate Actions`, `Fundamentals`, `Earnings`, `Market / Industry`, and
  `Portfolio Risk` are still placeholder modules unless backend support is
  added and verified.
- The dashboard should be extended conservatively inside the current UI
  structure, not replaced by a standalone trading terminal.
- Trade execution must stay gated. Ambiguous or incomplete evidence should
  downgrade actions to `REVIEW` rather than producing executable instructions.

## Long-Term Outcome

For each holding, the plugin should combine:

- TradingAgents proposed action and thesis.
- Futu real-time quotes, K-line data, market state, account, position, and cash
  data.
- Futu news, notices, research, stock digest, and community sentiment.
- Futu technical anomaly, capital-flow anomaly, and derivatives anomaly signals.
- Local portfolio context such as weight, broker source, risk flag, and planned
  trade action.

The final review layer should classify the proposed action as one of:

- `executable`: evidence is current enough and no module blocks execution.
- `review`: evidence is missing, stale, conflicting, or too ambiguous.
- `reduce_only`: risk supports reducing exposure but does not support adding.
- `wait_for_event`: earnings, announcement, corporate action, or market event
  should be confirmed before acting.

## Common Facts Contract

Future Futu-backed modules should publish a normalized facts object rather than
free-form prose. A working contract can start as:

```json
{
  "schema_version": "open_trader.futu_skill_facts.v1",
  "run_date": "2026-07-01",
  "market": "US",
  "symbol": "NVDA",
  "module": "news_sentiment",
  "status": "ok",
  "signal": "supportive",
  "confidence": "medium",
  "freshness": {
    "generated_at": "2026-07-01T09:10:00+08:00",
    "source_window": "latest"
  },
  "evidence": [
    {
      "title": "Short evidence title",
      "summary": "Concise Chinese evidence summary.",
      "url": "https://example.com/source"
    }
  ],
  "blocking_reason": "",
  "suggested_constraint": ""
}
```

Expected enums:

- `status`: `ok`, `partial`, `missing`, `error`, `stale`
- `signal`: `supportive`, `opposing`, `neutral`, `risk_up`, `mixed`
- `confidence`: `high`, `medium`, `low`
- `suggested_constraint`: empty, `review`, `reduce_only`, `wait_for_event`,
  `no_add`

The exact schema can be revised during the first implementation slice, but the
principle should remain: module outputs must be structured, auditable, and
usable by both UI cards and action-gating logic.

## Phased Roadmap

### Phase 1: Facts Layer And News / Sentiment

Build the common facts contract and connect the `News / Sentiment` plugin to
Futu Skills:

- `futu-news-search` for recent news, notices, and research links.
- `futu-stock-digest` for single-stock directional interpretation based on
  retrieved public information.
- `futu-comment-sentiment` for community bullish / bearish / neutral tone.

This phase should only affect the `News / Sentiment` card and any supporting
facts artifact. It should not change order generation.

Success criteria:

- A dated and latest facts artifact exists for each market/symbol.
- The dashboard shows Futu-backed news/sentiment evidence with source links.
- Missing or failed retrieval renders as `partial`, `missing`, or `error`
  without fabricating conclusions.
- Trading actions are not automatically changed yet; this phase validates the
  contract and UI behavior.

### Phase 2: Event Blocking

Add event modules that can block or constrain execution:

- `Corporate Actions`: dividends, buybacks, splits, suspensions, offerings.
- `Earnings`: upcoming earnings date, historical earnings-day volatility, and
  post-earnings review requirements.

The target behavior is to produce constraints such as `wait_for_event`,
`review`, or `reduce_only` when the facts are relevant.

### Phase 3: Portfolio Risk

Convert the current `Portfolio Risk` placeholder into a real risk module using
local portfolio data plus Futu quotes/account data.

Initial rules should be simple and explicit:

- single-symbol weight above threshold
- market value or quote missing
- cash unavailable for planned buy
- high volatility plus high position weight
- broker/account data incomplete

This phase may begin influencing action generation by downgrading unsafe rows
to `REVIEW`.

### Phase 4: Technical, Capital, And Derivatives Anomalies

Use Futu anomaly skills as evidence modules:

- `futu-technical-anomaly` for K-line pattern and indicator anomaly checks.
- `futu-capital-anomaly` for capital flow, broker flow, and short-selling
  anomaly checks.
- `futu-derivatives-anomaly` for options, IV, unusual option activity, and Hong
  Kong warrant signals where applicable.

These signals should strengthen or challenge the TradingAgents thesis, but they
should not override risk gates by themselves.

### Phase 5: Fundamentals And Market / Industry

Use Futu fundamentals, valuation, analyst, Morningstar, plate, index, and
industry APIs to support slower-moving review:

- valuation support or pressure
- earnings and revenue trend
- analyst consensus changes
- sector/index environment
- peer or industry valuation context

These modules should mainly affect medium-term holding confidence, not intraday
execution.

## Recommended First Slice

Start with Phase 1 only:

1. Define `open_trader.futu_skill_facts.v1`.
2. Add a news/sentiment facts generator for one market at a time.
3. Write dated artifacts under `data/runs/<YYYY-MM-DD>/<MARKET>/`.
4. Optionally promote to `data/latest/<MARKET>/`.
5. Render the existing `News / Sentiment` card from the new facts artifact.
6. Keep TradingAgents and order/action generation unchanged.

This keeps the first implementation small, validates the common contract, and
creates a reusable pattern for later modules.

## Out Of Scope For The First Slice

- Real-money order placement.
- Automatic order execution.
- Changing generated trade actions based on Futu facts.
- Building every placeholder module.
- Replacing the current dashboard layout.
- Inventing scores or target prices not grounded in retrieved Futu data.

## Restart Notes For Future Sessions

When resuming this work, begin here:

1. Read this document.
2. Inspect the current dashboard plugin code in
   `src/open_trader/dashboard_static/dashboard.js`.
3. Inspect dashboard payload assembly in `src/open_trader/dashboard.py`.
4. Re-check current Futu skill availability under `/Users/ray/.codex/skills`.
5. Start with Phase 1 unless the user explicitly chooses another phase.

The next design topic should be:

`Futu Skills facts layer and News / Sentiment plugin enhancement`
