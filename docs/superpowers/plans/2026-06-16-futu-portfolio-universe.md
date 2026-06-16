# Futu Portfolio Universe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a testable portfolio-to-Futu quote universe that excludes cash and money market funds.

**Architecture:** Add a focused module that reads portfolio CSV rows and returns quoteable Futu symbols plus skipped-row reasons. Keep Futu SDK access in `futu_quote.py`; this module only prepares symbols.

**Tech Stack:** Python standard library CSV/Decimal, pytest.

---

### Task 1: Portfolio Universe Filtering

**Files:**
- Create: `src/open_trader/futu_universe.py`
- Create: `tests/test_futu_universe.py`

- [ ] **Step 1: Write failing tests**

Create tests for supported US/HK rows, cash exclusion, money-market-fund exclusion, invalid quantity exclusion, and HK symbol normalization.

- [ ] **Step 2: Run focused tests**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/test_futu_universe.py -q`

Expected: fail because `open_trader.futu_universe` does not exist.

- [ ] **Step 3: Implement the module**

Create `FutuUniverseItem`, `SkippedFutuUniverseRow`, and `load_futu_quote_universe(portfolio_path)`.

- [ ] **Step 4: Run focused tests**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/test_futu_universe.py -q`

Expected: pass.

- [ ] **Step 5: Run full tests**

Run: `PYTHONPATH=src .venv/bin/python -m pytest -q`

Expected: pass.
