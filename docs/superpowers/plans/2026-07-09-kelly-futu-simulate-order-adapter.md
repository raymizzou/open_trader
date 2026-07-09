# Kelly Futu Simulate Order Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only Futu simulate-order adapter for Kelly Lab paper-order sync.

**Architecture:** Extend `kelly_paper_order_sync.py` with a Futu simulate client that uses `OpenSecTradeContext` only for account and order queries. The client loads Kelly experiment participants from `data/latest/kelly_experiments.json`, maps Futu order symbols to a unique experiment, and writes the existing `kelly_paper_orders.json` artifact through `sync_kelly_paper_orders()`.

**Tech Stack:** Python, futu-api `OpenSecTradeContext`, argparse, pytest fakes, existing Kelly Lab Playwright verification.

---

## Files

- Modify: `src/open_trader/kelly_paper_order_sync.py`
  - Add `FutuPaperOrderSyncError`.
  - Add `FutuSimulatePaperOrderClient`.
  - Add `load_kelly_experiment_symbol_index()`.
  - Normalize Futu order rows into existing order artifact shape.
- Modify: `src/open_trader/cli.py`
  - Change `kelly sync-paper-orders` to accept exactly one of `--fake` or `--futu-simulate`.
  - Add `--host` and `--port` for the Futu OpenD connection.
- Modify: `tests/test_kelly_paper_order_sync.py`
  - Add fake Futu context tests for account filtering, order normalization, and non-unique symbol attribution.
- Modify: `tests/test_kelly_paper_order_sync_cli.py`
  - Add parser and CLI wiring tests for `--futu-simulate`.

## Task 1: Adapter Tests

- [ ] **Step 1: Write failing tests**

Add tests that prove:

- A Futu simulate account is selected from `get_acc_list()`.
- `order_list_query()` is called with `trd_env="SIMULATE"`.
- Futu `US.RAM` order rows map to `market="US"`, `symbol="RAM"`, and the experiment id from the Kelly experiment participant index.
- Symbols that match more than one experiment are skipped.

- [ ] **Step 2: Verify red**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_paper_order_sync.py -q
```

Expected: import or attribute failures because the Futu simulate adapter does not exist.

## Task 2: Adapter Implementation

- [ ] **Step 1: Implement the read-only adapter**

Use the same dependency injection pattern as `FutuAccountClient`: a context factory and connectivity checker make tests deterministic.

- [ ] **Step 2: Verify green**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_paper_order_sync.py -q
```

Expected: all tests pass.

## Task 3: CLI Wiring

- [ ] **Step 1: Add failing CLI tests**

Cover `kelly sync-paper-orders --futu-simulate --host 127.0.0.1 --port 11111`.

- [ ] **Step 2: Wire CLI**

Use a mutually exclusive group for `--fake` and `--futu-simulate`.

- [ ] **Step 3: Verify CLI tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_paper_order_sync_cli.py -q
```

Expected: all tests pass.

## Task 4: Regression

- [ ] **Step 1: Run focused backend tests**

```bash
.venv/bin/python -m pytest tests/test_kelly_paper_order_sync_cli.py tests/test_kelly_paper_order_sync.py tests/test_kelly_lab.py tests/test_dashboard.py tests/test_dashboard_web.py -q
```

- [ ] **Step 2: Run Kelly UI Playwright**

```bash
npm run test:e2e:kelly
```

## Self-Review

- No order placement methods are called.
- Live trading environment remains blocked by `sync_kelly_paper_orders()` because the adapter reports `SIMULATE`.
- Ambiguous experiment attribution is skipped instead of guessed.
