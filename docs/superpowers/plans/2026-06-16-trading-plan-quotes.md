# Trading Plan Quotes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build structured trading plans from trader advice and evaluate live Futu quotes against those plans.

**Architecture:** Add a pure `trading_plan` module for CSV parsing, structured extraction, atomic writes, and quote-status evaluation. Wire two thin CLI commands: `build-trading-plan` and `check-futu-plan`. Reuse `FutuQuoteClient` for live snapshots.

**Tech Stack:** Python standard library CSV/Decimal/regex, pytest, existing Futu OpenD quote client.

---

### Task 1: Trading Plan Builder

**Files:**
- Create: `src/open_trader/trading_plan.py`
- Create: `tests/test_trading_plan.py`
- Modify: `src/open_trader/cli.py`
- Create/modify: `tests/test_trading_plan_cli.py`

- [ ] **Step 1: Write failing tests**

Cover structured MSFT-style advice conversion, failed advice preservation,
latest promotion behavior, dry-run behavior, and CLI wiring.

- [ ] **Step 2: Run focused tests**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/test_trading_plan.py tests/test_trading_plan_cli.py -q`

Expected: fail because `open_trader.trading_plan` and CLI commands do not exist.

- [ ] **Step 3: Implement builder**

Create `build_trading_plan(advice_path, data_dir, run_date=None, update_latest=True)`
and write dated/latest CSVs.

- [ ] **Step 4: Verify builder**

Run focused tests until they pass.

### Task 2: Futu Plan Check

**Files:**
- Modify: `src/open_trader/trading_plan.py`
- Modify: `src/open_trader/cli.py`
- Modify: `tests/test_trading_plan.py`
- Modify: `tests/test_trading_plan_cli.py`

- [ ] **Step 1: Write failing tests**

Cover `entry_zone`, `add_zone`, `stop_loss_hit`, `target_1_hit`,
`target_2_hit`, `watch`, and missing quote output.

- [ ] **Step 2: Implement evaluator**

Create `evaluate_plan_quote(plan, quote)` and wire `check-futu-plan`.

- [ ] **Step 3: Verify**

Run focused tests and full tests. Then run a local dry command against any
available dated `trading_advice.csv`.
