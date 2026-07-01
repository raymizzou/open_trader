# Changelog

Every push to `main` must add one dated entry here. Keep entries short and
operator-facing: what changed, which workflow is affected, and what was verified.

## 2026-07-02

- Reworked the dashboard holdings table around the operator fields: quantity,
  cost price, live price, USD/HKD market value, portfolio weight, and P/L.
- Split holdings into clear `US` then `HK` sections, removed the broker/action
  columns from the main table, and kept broker context inside the trading
  decision detail.
- Verified the dashboard with full pytest plus Playwright desktop/mobile checks
  for section order, market filters, horizontal scrolling, and detail expansion.

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
