# Changelog

Every push to `main` must add one dated entry here. Keep entries short and
operator-facing: what changed, which workflow is affected, and what was verified.

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
