# Trader Advice Template Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Format every per-symbol TradingAgents decision into the same trader advice template.

**Architecture:** Keep the existing `TradingAdvice` CSV contract unchanged. Add a small formatter that converts structured TradingAgents markdown into a Chinese template and falls back to raw text when the decision is not structured.

**Tech Stack:** Python standard library regex, pytest.

---

### Task 1: Template Formatter

**Files:**
- Create: `src/open_trader/advice/trader_template.py`
- Create: `tests/test_trader_template.py`
- Modify: `tests/test_tradingagents_adapter.py`

- [x] **Step 1: Write failing adapter test**

Assert that a TradingAgents response with `Rating`, `Executive Summary`,
`Investment Thesis`, `Price Target`, and `Time Horizon` becomes the normalized
template in `advice_summary`.

- [x] **Step 2: Verify red**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/test_tradingagents_adapter.py -q`

Expected: fail because `advice_summary` still contains raw markdown.

- [x] **Step 3: Implement formatter and adapter integration**

Create `format_trader_template(final_trade_decision, action)` and call it from
`TradingAgentsAdapter._extract_summary`.

- [x] **Step 4: Add fallback tests**

Cover unstructured text and missing `Rating`.

- [x] **Step 5: Verify**

Run focused tests and full tests.
