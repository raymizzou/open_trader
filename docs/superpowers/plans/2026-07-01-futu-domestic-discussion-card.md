# Futu Domestic Discussion Card Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep the existing TradingAgents news/sentiment fields and add a fixed-field Futu domestic discussion section inside the same card.

**Architecture:** `futu_skill_facts` will expose a `domestic_discussion` object inside the existing `news_sentiment` module, derived only from Futu `stock_feed` posts. The dashboard will continue rendering the original `decision_facts.news_sentiment` rows, then append a compact Futu section with fixed fields and no raw URL evidence grid.

**Tech Stack:** Python 3.12, pytest, existing dashboard JavaScript rendered through Node VM tests.

---

### Task 1: Data Contract For Domestic Discussion

**Files:**
- Modify: `src/open_trader/futu_skill_facts.py`
- Test: `tests/test_futu_skill_facts.py`

- [x] **Step 1: Write failing extractor test**

Add expectations that `FutuNewsSentimentExtractor.extract_news_sentiment()` returns `domestic_discussion` with fixed fields:

```python
assert result["domestic_discussion"] == {
    "status": "ok",
    "direction": "bullish",
    "quality": "usable",
    "representative_view": "继续看好 DRAM ETF",
    "risk_point": "未见明确国内风险点",
    "constraint": "富途社区讨论仅作国内讨论温度参考，不单独作为交易依据",
    "post_count": 2,
    "relevant_post_count": 2,
}
```

- [x] **Step 2: Run focused test and verify it fails**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_futu_skill_facts.py::test_futu_news_sentiment_extractor_builds_evidence_from_futu_apis -v
```

Expected: FAIL because `domestic_discussion` is missing.

- [x] **Step 3: Implement domestic discussion extraction**

Add source tagging to evidence and derive `domestic_discussion` from `stock_feed` only. Keep noisy feed as `quality=noisy`, `direction=noisy`, and never use news links as community views.

- [x] **Step 4: Verify data tests pass**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_futu_skill_facts.py -v
```

Expected: PASS.

### Task 2: Dashboard Rendering

**Files:**
- Modify: `src/open_trader/dashboard.py`
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Test: `tests/test_dashboard.py`
- Test: `tests/test_dashboard_web.py`

- [x] **Step 1: Write failing dashboard tests**

Add tests proving:

- `futu_skill_facts.news_sentiment.domestic_discussion` reaches the dashboard payload.
- The `新闻 / 舆论` card still renders original `decision_facts.news_sentiment` values.
- The card appends `富途社区 / 国内讨论` with fixed fields.
- The card does not render `Futu Skill 证据` or long URL evidence rows.

- [x] **Step 2: Run focused tests and verify they fail**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_futu_skill_facts.py tests/test_dashboard.py::test_load_dashboard_state_attaches_futu_skill_facts tests/test_dashboard_web.py::test_dashboard_renders_fixed_decision_fact_cards_in_chinese -v
```

Expected: FAIL before implementation.

- [x] **Step 3: Implement combined news card**

Replace `futuSkillNewsSentimentPlugin(holding) || decisionFactsPlugin(...)` with a combined news plugin that starts from `decisionFactsPlugin(...)` and appends a Futu domestic section when available.

- [x] **Step 4: Verify focused tests pass**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_futu_skill_facts.py tests/test_dashboard.py tests/test_dashboard_web.py -v
```

Expected: PASS.

### Task 3: Local Verification

**Files:**
- Runtime artifacts under `data/latest/<market>/futu_skill_facts.json`

- [x] **Step 1: Regenerate Futu facts for current portfolio**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m open_trader extract-futu-skill-facts --portfolio data/latest/portfolio.csv --data-dir data --date 2026-07-01 --market US --update-latest
PYTHONPATH=src .venv/bin/python -m open_trader extract-futu-skill-facts --portfolio data/latest/portfolio.csv --data-dir data --date 2026-07-01 --market HK --update-latest
```

- [x] **Step 2: Restart local dashboard and verify with Playwright**

Run dashboard on a fresh local port and click a trading-decision row. Confirm `新闻 / 舆论`, original TradingAgents fields, and `富途社区 / 国内讨论` are visible.

- [x] **Step 3: Commit**

Commit code and plan only; do not commit generated runtime data unless explicitly requested.
