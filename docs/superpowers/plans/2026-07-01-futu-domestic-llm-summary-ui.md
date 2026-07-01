# Futu Domestic LLM Summary UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Replace raw Futu community text in the dashboard with LLM-summarized fixed fields and render those fields as a single-column section.

**Architecture:** Extend `open_trader.futu_skill_facts.v1` domestic discussion fields from raw heuristics to an LLM summary contract. The extractor will still fetch Futu `news_search` and `stock_feed`, filter relevant community posts, then ask a summarizer to produce five fixed Chinese fields; the UI will render only those fields in a single-column section.

**Tech Stack:** Python 3.12, OpenAI-compatible DeepSeek client already used by the repo, pytest, existing dashboard JavaScript and CSS.

---

### Task 1: LLM Domestic Discussion Contract

**Files:**
- Modify: `src/open_trader/futu_skill_facts.py`
- Test: `tests/test_futu_skill_facts.py`

- [x] **Step 1: Write failing tests for LLM summarized domestic fields**

Add tests that prove `FutuNewsSentimentExtractor` accepts a `domestic_summarizer` dependency and returns `domestic_discussion` with:

- `summary`
- `focus`
- `divergence_risk`
- `credibility`
- `trading_constraint`
- `post_count`
- `relevant_post_count`

- [x] **Step 2: Verify tests fail**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_futu_skill_facts.py::test_futu_news_sentiment_extractor_uses_llm_domestic_discussion_summary tests/test_futu_skill_facts.py::test_llm_domestic_discussion_summarizer_sends_fixed_schema_prompt -v
```

Expected: FAIL because the summarizer interface and fixed fields do not exist.

- [x] **Step 3: Implement summarizer interface and validation**

Add:

- `FutuDomesticDiscussionSummarizer` protocol
- `LLMFutuDomesticDiscussionSummarizer`
- `OpenAITextClient` reuse/import pattern matching `decision_facts.py`
- validation for the five fixed Chinese fields
- deterministic fallback for missing/noisy cases when LLM fails

- [x] **Step 4: Verify data tests pass**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_futu_skill_facts.py -v
```

Expected: PASS.

### Task 2: Dashboard Payload And UI

**Files:**
- Modify: `src/open_trader/dashboard.py`
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `src/open_trader/dashboard_static/dashboard.css`
- Test: `tests/test_dashboard.py`
- Test: `tests/test_dashboard_web.py`

- [x] **Step 1: Write failing UI tests**

Update tests so the `新闻 / 舆论` card keeps the original TradingAgents five fields and appends a single-column `富途社区 / 国内讨论` section with:

- `国内讨论结论`
- `主要关注点`
- `分歧 / 风险`
- `可信度`
- `交易约束`

Also assert old labels like `代表观点`, `国内风险点`, `数据约束`, and raw URL evidence are absent.

- [x] **Step 2: Verify UI tests fail**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_dashboard.py::test_load_dashboard_state_attaches_futu_skill_facts tests/test_dashboard_web.py::test_dashboard_renders_fixed_decision_fact_cards_in_chinese tests/test_dashboard_web.py::test_dashboard_static_assets_include_local_shell -v
```

Expected: FAIL because the UI still uses the previous field names and grid layout.

- [x] **Step 3: Implement single-column domestic section**

Normalize the dashboard payload to the new field names. Render only the LLM summary fields in a single-column `.domestic-list`, following the approved mockup.

- [x] **Step 4: Verify focused tests pass**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_futu_skill_facts.py tests/test_dashboard.py tests/test_dashboard_web.py -v
```

Expected: PASS.

### Task 3: Runtime Verification

**Files:**
- Runtime data: `data/latest/<market>/futu_skill_facts.json`

- [x] **Step 1: Regenerate Futu facts for US and HK**

Run both markets with `--update-latest`.

- [x] **Step 2: Deploy dashboard to a fresh local port**

Start a new `screen` session on a fresh port, then use Playwright to open DRAM's trading decision page.

- [x] **Step 3: Verify UI and commit**

Confirm original TradingAgents news fields remain, Futu domestic section is single-column, raw URLs are absent, and tests pass. Commit only code/tests/plans/mockup files related to this change.
