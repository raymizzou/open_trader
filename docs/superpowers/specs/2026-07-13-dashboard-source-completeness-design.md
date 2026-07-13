# Dashboard Source Completeness Design

## Goal

Make every expected decision data source current and usable for all symbols in
the current market-scoped TradingAgents advice, give TradingAgents its own tab,
and make Dashboard acceptance fail whenever any required source is unavailable.

Only a final `make acceptance` result of `PASS` permits the Dashboard to be
presented for user verification.

## Root Cause

The current daily runner explicitly injects skipped generators for technical
facts and decision facts. It also defaults the TradingAgents summary generator
to a skipped result. The resulting artifacts use
`reason=daily_premarket_non_blocking` and contain no records.

Skipped technical and decision facts are not promoted, so their July 6 latest
caches remain on disk after July 10 advice is published. The Dashboard correctly
rejects them because their source hashes do not match current advice. Skipped
TradingAgents summaries are promoted unconditionally, replacing usable latest
summaries with empty records.

Futu community sentiment is a separate source. For DRAM it is currently usable,
but the fixed news-decision fields in the same tab are stale and unavailable.
The mixed state makes the tab look partially missing even though community data
was collected.

The existing acceptance validator checks portfolio totals, broker data, browser
loading, process identity, logs, and refresh stability, but does not check
decision-source availability. It therefore returns `PASS` while every current
advice symbol lacks TradingAgents summary and K-line facts.

## Scope

Strict source completeness applies to every symbol present in current:

```text
data/latest/US/trading_advice.csv
data/latest/HK/trading_advice.csv
```

This excludes holdings that do not participate in TradingAgents advice, such as
options and current A-share statement-only holdings.

## Daily Schedule

Keep the two existing launchd jobs and their current local-machine schedule:

- `com.open-trader.premarket.hk`: weekdays at 08:00 Asia/Shanghai.
- `com.open-trader.premarket.us`: weekdays at 18:30 Asia/Shanghai.

Do not add another scheduled job, background watcher, or facts-job service.

## Synchronous Data Pipeline

Each existing daily run performs one ordered workflow:

1. Refresh portfolio data.
2. Generate TradingAgents advice.
3. Generate technical facts.
4. Generate fixed decision facts.
5. Generate TradingAgents summaries.
6. Generate Futu community sentiment and technical, capital, and derivatives
   anomaly facts.
7. Build plans and trade actions.
8. Publish the complete latest artifact set.

Use the existing extractors and artifact schemas. Remove the skipped-generator
defaults from the production daily path. Tests may continue injecting fake
generators explicitly.

Only artifacts generated from the current advice may be promoted. Existing
source-hash validation remains authoritative. Do not accept or relabel an older
cache as current.

## Failure And Recovery

If any required source fails for any in-scope symbol:

- Set the daily run status to `failed`.
- Record market, symbol, source name, source status, and available error text in
  `daily_run_status.json` and the daily report.
- Send a Feishu blocker notification containing the failing sources and the
  relevant retry command.
- Do not overwrite a previously usable facts cache with an unusable artifact.
- Do not let the old cache pass acceptance when its source hash or run identity
  does not match current advice.

After correcting an external problem, rerun the existing full market job:

```bash
launchctl kickstart -k gui/$(id -u)/com.open-trader.premarket.hk
launchctl kickstart -k gui/$(id -u)/com.open-trader.premarket.us
```

Do not require the user to invoke multiple extractor commands manually.

## Trading Decision Tabs

The tab order becomes:

1. `最终决策`
2. `TradingAgents`
3. `趋势 / K 线`
4. `新闻 / 舆论`
5. `富途异动`

`最终决策` contains only the existing execution-review template.
`TradingAgents` becomes a separate panel showing TA view, current action, core
reason, TA report date, and latest run date.

Keep the current content width, natural panel height, default selection reset,
and horizontally scrollable mobile tab row. An unavailable tab remains visible,
clickable, red, and displays its real failure reason.

## Strict Source Contract

For every in-scope symbol, acceptance requires each source independently:

- `tradingagents_summary.available == true`
- `technical_facts.available == true`
- `decision_facts.kline.available == true`
- `decision_facts.news_sentiment.available == true`
- `futu_skill_facts.news_sentiment.available == true`
- `futu_skill_facts.technical_anomaly.available == true`
- `futu_skill_facts.capital_anomaly.available == true`
- `futu_skill_facts.derivatives_anomaly.available == true`

This is deliberately stricter than tab-level fallback. One available source
must not conceal another failed source.

## Acceptance Gate

Extend `validate_dashboard_payload()` to derive the in-scope symbol set from
current market-scoped advice represented in the Dashboard payload and validate
the strict source contract. Each error must identify:

```text
<market>.<symbol>: <source> unavailable (<status or error>)
```

The Dashboard browser check must run on desktop and mobile and:

- Open a real in-scope holding's trading-decision detail.
- Confirm all five tabs exist in the fixed order.
- Confirm none has the failure class.
- Click every tab and confirm its panel is visible.
- Reject `数据未生成` in any selected panel.

Fixtures, mocks, curl-only checks, screenshots, and unit tests cannot substitute
for the real final gate.

## Verification

Implementation must include focused tests for:

- Production daily runs invoking real configured generators instead of skipped
  generators.
- Failure aggregation by market, symbol, and source.
- Latest promotion refusing unusable or stale facts.
- TradingAgents rendering as its own tab.
- Strict acceptance rejecting each unavailable source independently.
- Desktop and mobile five-tab interaction.

Before handoff:

1. Rebuild current US and HK decision sources with the fixed daily workflow.
2. Restart Dashboard processes still holding old code.
3. Verify fresh logs, PID, working directory, and current Git SHA.
4. Run `make acceptance` last.

Any result other than `PASS` is not complete and must not be presented for user
verification.
