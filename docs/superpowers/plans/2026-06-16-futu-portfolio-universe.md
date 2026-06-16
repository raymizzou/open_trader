# Futu Portfolio Universe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a testable portfolio-to-Futu quote universe that excludes cash and money market funds.

**Architecture:** Add a focused module that reads portfolio CSV rows and returns quoteable Futu symbols plus skipped-row reasons. Keep Futu SDK access in `futu_quote.py`; this module only prepares symbols. Add a thin CLI command that connects to OpenD and fetches one snapshot for each quoteable portfolio symbol.

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

### Task 2: Portfolio Quote Check CLI

**Files:**
- Modify: `src/open_trader/cli.py`
- Modify: `tests/test_futu_watch_cli.py`
- Modify: `docs/monthly_portfolio_import.md`

- [ ] **Step 1: Write failing CLI tests**

Add tests showing `check-futu-quotes --help` exists and `check-futu-quotes`
loads the Futu universe, sends sorted Futu symbols to `FutuQuoteClient`, prints
quotes, reports missing symbols, and prints skipped counts.

- [ ] **Step 2: Run focused CLI tests**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/test_futu_watch_cli.py -q`

Expected: fail because `check-futu-quotes` is not registered.

- [ ] **Step 3: Implement CLI command**

Register `check-futu-quotes` with `--portfolio`, `--host`, and `--port`. In the
handler, call `load_futu_quote_universe`, connect with `FutuQuoteClient`, fetch
snapshots for sorted unique symbols, print quote/missing lines, and close the
client in a `finally` block.

- [ ] **Step 4: Document the command**

Add a short `check-futu-quotes` usage example to
`docs/monthly_portfolio_import.md`, noting that cash and money market funds are
excluded.

- [ ] **Step 5: Run full tests and live verification**

Run: `PYTHONPATH=src .venv/bin/python -m pytest -q`

Run: `PYTHONPATH=src .venv/bin/python -m open_trader check-futu-quotes --portfolio data/latest/portfolio.csv`

Expected: tests pass, and the live command returns quotes without requesting
cash or money market fund codes.
