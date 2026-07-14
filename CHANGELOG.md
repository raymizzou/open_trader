# Changelog

Every push to `main` must add one dated entry here. Keep entries short and
operator-facing: what changed, which workflow is affected, and what was verified.

## 2026-07-14

- Grouped holdings by broker account with strategy-horizon labels, split account
  and whole-portfolio weights into separate columns, and added distinct low-
  saturation broker colors to account headers and strategy summaries while
  keeping holding tables white; verified merged `main` with `1622` tests and
  the full Dashboard acceptance gate (`PASS`) on a dedicated port.
- Added one daily decision plan per holding with a 10% position cap, repeatable
  condition notifications, mandatory benchmark backtest gates, and non-executable
  fallback evidence showing maximum drawdown, Sharpe, and Calmar ratios; Dashboard
  acceptance now rejects missing risk metrics or K-line current prices.
- Replaced AKShare with Futu OpenD as the sole A-share real-time and historical
  market-data source across Dashboard quotes, backtests, watches, and T signals;
  verified 26/26 live quotes and the full Dashboard acceptance gate (`PASS`).

## 2026-07-13

- Refreshed the Dashboard command-center styling without changing its displayed
  data contract, and added configurable acceptance URL/log settings so isolated
  worktrees can be verified on a separate port.
- Replaced the stale Phillips snapshot with the latest archived 2026-07-10
  statement, using its authoritative HKD base cash total and excluding closed
  zero-value positions; the Dashboard now reports HKD 628,554.06 total assets.
- Made Dashboard acceptance verify the latest archived Phillips PDF instead of
  fixed portfolio row counts, preserved partial-source results with visible
  failures, and verified merged `main` with `1504` tests plus desktop/mobile
  acceptance (`PASS`).

## 2026-07-12

- Added optional Eastmoney statement path and PDF password loading from the
  existing local premarket environment file, while keeping explicit CLI paths
  authoritative and secrets outside version control.
- Imported the encrypted Eastmoney statement into the unified portfolio source,
  restoring five A-share holdings and one CNY cash row alongside the existing
  broker data.
- Restarted the live Dashboard on port `8766` and verified the merged `main`
  with `1445` passing tests plus desktop/mobile Playwright acceptance (`PASS`)
  against all 33 portfolio rows.
- Kept pending Kelly exits available when unified strategy stats are missing,
  malformed, stale, or incomplete, while suppressing entries until stats recover.
- Bound entry risk approval to the current validated trade evidence and strategy
  stats through exact timestamps, parameter provenance, and a canonical SHA-256
  evidence digest; restored the original two-decimal trade-sample rounding rules.
- Required unified strategy stats to cover every currently configured experiment
  before any entry can pass risk, while preserving exit approval on config/stats
  failures and isolating provenance validation from optional order artifacts.
- Changed pending-entry lifecycle and intent text to state that sizing and risk are
  still pending, removing pre-risk percentage and approval claims from artifacts
  and the dashboard.

## 2026-07-11

- 将 Kelly 交易证据与运行时 `kelly_strategy_stats.json` 分离，让仪表盘与订单
  仓位统一使用同一策略统计源，并在统计缺失、无效、过期或不完整时关闭入场
  路径（fail closed）。
- Completed the Kelly trade-sample closed loop on `main`: synced paper orders can
  now generate `kelly_trade_samples.json`, overlay per-strategy sample stats in
  Kelly Lab, and show the parameter source plus skipped-order count in the
  dashboard.
- Kept sample artifacts out of producer command dependencies so rebuilding order
  intents, strategy capital, or trade samples is not blocked by stale/corrupt
  sample stats.
- Verified on merged `main` with focused Kelly/dashboard pytest coverage
  (`134 passed`), Kelly Playwright (`1 passed`), `compileall`, `git diff --check`,
  and a restarted live dashboard on port `8766`.

## 2026-07-10

- Fixed the daily US/HK premarket workflow so non-dry-run automation refreshes
  the live Futu and Tiger portfolio before generating premarket advice and trade
  actions, preventing stale holdings from producing false manual-review
  blockers.
- Changed single-share trim sizing so a triggered `TRIM` action on a 1-share
  holding produces a 1-share ready action instead of rounding to zero and
  requiring manual review.
- Verified on local `main` with the full pytest suite, replayed the 2026-07-09
  US blocker scenario as `ready=2 review=0`, and confirmed the US launchd
  premarket job was not running stale code.
- Added the Kelly strategy lab workflow for paper-trading experiments, including
  strategy details, symbol-level lifecycle states, Kelly parameter derivation,
  risk-checked order intents, execution records, and Futu order linkage.
- Connected Futu SIMULATE order execution and order sync so submitted paper
  orders can be attributed back to strategy samples and used for future Kelly
  parameter updates.
- Added explicit Futu trading-market selection for HK, US, and CN simulate
  accounts so paper-order sync and execution target the intended market account.
- Enforced single-market Kelly paper experiments with fixed per-strategy
  simulated budgets of `30000 USD`, `200000 HKD`, and disabled `150000 CNY`,
  split mixed-market mock data, and blocked cross-market order intents before
  execution.
- Added automatic Futu SIMULATE market routing for Kelly paper-order sync and
  execution so commands follow experiment/order markets by default while still
  allowing manual `--trd-market` overrides.
- Added strategy-level Kelly capital snapshots, capital-aware order risk checks,
  and a Kelly Lab capital panel showing occupied, available, and next-order
  impact per strategy.
- Added Kelly trade sample generation from synced Futu paper orders, including
  derived win rate, payoff ratio, Kelly sizing stats, and dashboard source
  visibility.
- Verified with focused Kelly/dashboard pytest coverage, compile checks,
  `git diff --check`, live Futu SIMULATE HK order execution/sync, and live
  US/CN simulate-account order probes.
- Added a mandatory `make acceptance` Dashboard gate with PASS/FAIL/BLOCKED
  results across tests, real data, refresh stability, process version, logs,
  and desktop/mobile Chrome flows; fixed OTHER holdings breaking Dashboard loads
  and Tiger refreshes converting preserved CN rows to OTHER. The gate now also
  checks the full 33-row portfolio, seven Phillips-linked rows, and the exact
  Eastmoney statement total; live broker refreshes fail closed and restore the
  prior CSV if they would remove another broker's holdings. Browser verification
  ignores Chrome's unattributed favicon 404 while still failing every observed
  business API or page-resource HTTP error.
- Fixed newer single-broker imports hiding older brokers' account details by
  loading the latest detail snapshot per broker; acceptance now rejects an
  empty Phillips account card in both the API payload and rendered page.
- Added password-prompted Eastmoney A-share statement imports using an explicit
  month-end CNY/HKD rate, plus AKShare daily prices for standard-strategy research.
- Kept the Dashboard holdings layout unchanged while adding the existing A-share
  market and Eastmoney broker filters.
- Added one global dashboard workspace for read-only standard-strategy research
  across current holdings and watchlist symbols, with trend-pullback,
  breakout-momentum, and range-mean-reversion strategies.
- Added buy-and-hold and market-index comparisons, explicit actual data dates,
  fixed cost and sizing assumptions, and standalone auditable artifacts.
- Preserved real nonzero Futu daily volume for breakout research and fixed the
  price/action chart to render the serialized close-price series.
- Verified with `192` focused and `1134` full pytest tests, three fresh real 1Y
  MSFT/Futu API runs with 320 positive-volume MSFT and SPY rows, and separate
  Playwright submissions for all three strategies proving visible equity,
  price-path, and action-marker geometry with no console or network errors.

## 2026-07-11

- Added a dashboard backtest price-sync status line so operators can see when
  automatic price backfill succeeds or fails during page load.

## 2026-07-10

- Added a dashboard action to fetch missing backtest price CSVs from Futu daily
  K-line data and refresh the per-holding backtest readiness state.
- Marked sell-side, hold, and underweight trading plans as unsupported by the
  first buy-side backtest engine instead of showing misleading missing fields.
- Added sell-side trading-plan backtests for underweight/reduce/trim/sell
  ratings, seeded from current dashboard holding quantity and verified through
  pytest plus a local dashboard click check.
- Added a dashboard backtest-status filter so operators can isolate holdings
  that are ready to run, missing prices, missing plan fields, or unsupported.
- Added live counts to the dashboard backtest-status filter, scoped by the
  current market and broker filters.
- Made dashboard loads automatically fetch missing backtest daily K-line price
  CSVs through Futu so operators do not need to manually fill price data first.
- Removed the manual backtest price-fetch button from the dashboard detail view;
  missing price data is now handled by automatic dashboard loading.

## 2026-07-09

- Added a read-only `run-backtest` MVP for active trading-plan rows, producing
  trades, equity curve, metrics, and Markdown report artifacts without updating
  `data/latest` or placing orders.
- Added dashboard backtest entry buttons that open a per-holding回测详情 view
  without showing backtest metrics on the main holdings table.
- Added a dashboard-only backtest run action that uses the local latest trading
  plan and `data/prices/<market>/<symbol>.csv`, then refreshes the detail view.
- Added dashboard backtest readiness details so operators can see missing plan
  fields and price CSV paths before running a backtest.
- Documented the first backtest workflow in both READMEs.
- Verified with focused backtest/dashboard pytest coverage, the full pytest
  suite, and a local dashboard click check on `127.0.0.1:8766`.

## 2026-07-04

- Added Futu daily-K Bollinger fact generation for dashboard K-line cards, fixed
  Futu/Tiger live-sync asset-class inference for type-less positions, and
  removed the duplicate technical-fact grid from those cards after live
  dashboard verification across all current HK/US eligible holdings.
- Added a fixed Bollinger-band display in the dashboard K-line card, with red
  upper-band risk, green lower-band opportunity, and neutral middle-range
  states.
- Backfilled Bollinger facts from real HK/US latest TradingAgents reports when
  model extraction fails, and verified the live dashboard renders those facts
  without `undefined`.
- Stabilized the daily HK/US premarket workflow around `portfolio.csv` holdings,
  report-symbol filtering, non-blocking facts/summary artifacts, configurable
  worker concurrency, and Feishu start/completion notifications.
- Verified with the full pytest suite and `git diff --check`.

## 2026-07-03

- Added holdings-table 做T signal details with fixed ratio sizing, signal
  evidence, precondition checks, notification timeline, and session-gated pulse
  highlighting.
- Enabled HK 做T signal generation through Futu realtime subscriptions for
  1-minute K lines, 5-minute K lines, and order book data.
- Changed 做T Feishu alerts to one structured Chinese message per symbol with
  action, ratio, status, conclusion, numbered evidence, and timestamp.
- Verified with the full pytest suite, Playwright against the local dashboard,
  live HK Futu signal generation, and a real Feishu app notification send.

## 2026-07-02

- Reworked the dashboard holdings table around the operator fields: quantity,
  cost price, live price, USD/HKD market value, portfolio weight, and P/L.
- Split holdings into `美股正股`, `美股期权`, `港股正股`, and `港股期权`
  sections, kept each section sorted by portfolio weight, and kept broker
  context inside the trading decision detail.
- Added the Futu anomaly signal card to the trading decision detail so
  technical, capital-flow, and derivatives anomaly signals display in Chinese
  without leaking raw enum/schema text.
- Verified with focused dashboard/Futu facts pytest, live local dashboard
  deployment on `127.0.0.1:8766`, and Playwright checks for section order,
  section weight sorting, detail expansion, and the anomaly signal card.

## 2026-07-01

- Fixed Phillips statement parsing for `UT OTCU` money-market-fund rows so the
  Phillip HKD Money Market Fund is included in monthly holdings refreshes.
- Refreshed the local Phillips monthly baseline from the 2026-06 statement and
  verified live Futu/Tiger sync preserves the updated statement rows.
- Verified with focused parser/account-sync tests and dashboard API checks for
  `2026-06 月结单导入`.

## 2026-06-30

- Canonicalized `portfolio.csv` grouping so daily HK/US workflows consume
  deduplicated current holdings instead of repeated broker rows.
- Hardened Futu and Tiger portfolio sync merges, including malformed cash rows,
  stale Tiger FX rows, mixed-broker fallback safety, and multi-broker cash detail
  preservation.
- Stabilized daily startup by clearing successful run locks and adding bounded
  OpenAI-compatible request timeouts for classifier, facts, and TradingAgents
  summary post-processing.
- Added blocker notifications when TradingAgents advice, trading plans, or
  summaries degrade to fallback/error so missing US reports are visible to the
  operator.
- Verified with live Futu/Tiger syncs, `data/latest/portfolio.csv` duplicate
  count `0`, US daily runner `success / ready`, local dashboard deployment on
  `127.0.0.1:8766`, Playwright desktop/mobile checks, and `832` passing tests.

## 2026-06-23

- Added fixed TradingAgents decision facts for dashboard display:
  `趋势 / K 线` uses `趋势`, `位置`, `动能`, `关键位`, `风险`;
  `新闻 / 舆论` uses `方向`, `变化`, `催化`, `风险`, `热度`.
- Added LLM extraction and validation for `decision_facts.json`, with per-module
  fallback to `缺失` when a module cannot be extracted safely.
- Wired decision facts into the daily premarket pipeline and dashboard payload,
  including source-hash freshness checks.
- Updated the local dashboard cards so missing fixed fields show `缺失` instead
  of explanatory filler or raw English TradingAgents prose.
- Documented local dashboard deployment on port `8766` and added structured API
  checks for `SOXX` decision facts.
