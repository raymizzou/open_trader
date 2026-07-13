# Dashboard Partial Source Publishing Design

## Decision

Dashboard decision data publishes by data source, not as an all-or-nothing daily batch. A successful source remains visible even when another source fails. Failed current data stays visible as a red failure with its real error.

Independent retries use the existing command-line extractors at market-and-source granularity. No second scheduler, queue, or per-symbol retry system is added.

## Scope

The current advice rows in `data/latest/<MARKET>/trading_advice.csv` define the symbols checked by the daily status and Dashboard acceptance gate.

The eight required sources are:

- `tradingagents_summary`
- `technical_facts`
- `decision_facts.kline`
- `decision_facts.news_sentiment`
- `futu_skill_facts.news_sentiment`
- `futu_skill_facts.technical_anomaly`
- `futu_skill_facts.capital_anomaly`
- `futu_skill_facts.derivatives_anomaly`

## Daily Publishing

The existing HK and US daily jobs continue to run synchronously. After the run:

- Every generated artifact is promoted to its source's latest path, including an artifact containing a mix of successful and failed records.
- Successful records render normally.
- Failed records render as unavailable with their current status and error.
- If a generator raises before producing an artifact, that source's previous latest file is retained. Current run-date and source-hash checks make it unavailable instead of displaying it as current.
- A failure in one source does not block promotion of generated artifacts from other sources.
- Filesystem promotion itself remains transactional for the paths being promoted so a write error does not leave half-written files.

The daily run status is `failed` with readiness `blocked` whenever any required source is unavailable. `source_failures` lists the exact market, symbol, canonical source name, and error. Notifications include the relevant retry command.

## Independent Retry

Retries reuse the existing extractors with `--update-latest`:

```bash
.venv/bin/python -m open_trader extract-technical-facts --advice data/latest/US/trading_advice.csv --data-dir data --date YYYY-MM-DD --market US --update-latest

.venv/bin/python -m open_trader extract-decision-facts --advice data/latest/US/trading_advice.csv --data-dir data --date YYYY-MM-DD --market US --update-latest

.venv/bin/python -m open_trader extract-tradingagents-summary --advice data/latest/US/trading_advice.csv --plan data/latest/US/trading_plan.csv --actions data/latest/US/trade_actions.csv --data-dir data --date YYYY-MM-DD --market US --update-latest

.venv/bin/python -m open_trader extract-futu-skill-facts --portfolio data/latest/portfolio.csv --data-dir data --date YYYY-MM-DD --market US --update-latest
```

HK uses the same commands with `HK` paths and market. Dependencies remain explicit:

- Technical and decision facts depend on current trading advice.
- TradingAgents summary depends on current advice, plan, and trade actions.
- Futu skill facts depend on the current portfolio and live Futu inputs.

Retrying a source does not rerun unrelated sources.

## Dashboard and Acceptance

Unavailable tabs remain present and red. Fallback display text, stale run dates, and stale source hashes never count as available.

`make acceptance` checks all eight sources for every current advice symbol on both API refresh cycles and in desktop/mobile five-tab flows. It returns `FAIL` until all current sources are available. Only `PASS` permits user review.

## Rejected Alternatives

- Whole-batch rollback hides successful current data when one independent source fails.
- Per-record merging with old latest data adds complexity and risks presenting stale records as successful.
- Per-symbol retry requires new filtering across four generators without a current operational need.
