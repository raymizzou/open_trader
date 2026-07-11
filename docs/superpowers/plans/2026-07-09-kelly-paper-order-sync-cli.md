# Kelly Paper Order Sync CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a command-line entry point for refreshing the Kelly paper-order artifact through the fake sync client.

**Architecture:** Keep the sync logic in `kelly_paper_order_sync.py` and wire only a thin argparse command in `cli.py`. The command is nested as `open-trader kelly sync-paper-orders --fake`, uses built-in mock orders for this phase, and prints the artifact path plus order count.

**Tech Stack:** Python argparse, existing `open_trader.cli`, pytest, existing Kelly Lab Playwright verification.

---

## Files

- Modify: `src/open_trader/kelly_paper_order_sync.py`
  - Add `default_fake_kelly_paper_orders()`.
- Modify: `src/open_trader/cli.py`
  - Add `kelly sync-paper-orders --fake`.
  - Call `sync_kelly_paper_orders()` with `FakeFutuPaperOrderClient`.
- Create: `tests/test_kelly_paper_order_sync_cli.py`
  - Cover parser acceptance.
  - Cover CLI wiring and printed summary.
  - Cover missing `--fake` rejection.

## Task 1: CLI Tests

- [ ] **Step 1: Write failing tests**

Create `tests/test_kelly_paper_order_sync_cli.py` with tests that call:

```python
cli.main([
    "kelly",
    "sync-paper-orders",
    "--fake",
    "--data-dir",
    str(tmp_path / "data"),
    "--synced-at",
    "2026-07-09 11:00",
])
```

Assert the CLI passes a fake client into `sync_kelly_paper_orders`, prints `orders: 1`, and prints `latest: .../kelly_paper_orders.json`.

- [ ] **Step 2: Verify red**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_paper_order_sync_cli.py -q
```

Expected: argparse rejects unknown `kelly` command.

## Task 2: CLI Implementation

- [ ] **Step 1: Add default fake orders**

Add `default_fake_kelly_paper_orders()` to `kelly_paper_order_sync.py`.

- [ ] **Step 2: Wire argparse**

Add a `kelly` parser with a nested `sync-paper-orders` subcommand. Require `--fake` for this phase.

- [ ] **Step 3: Verify green**

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_paper_order_sync_cli.py tests/test_kelly_paper_order_sync.py -q
```

Expected: all tests pass.

## Task 3: Regression

- [ ] **Step 1: Run focused backend tests**

```bash
.venv/bin/python -m pytest tests/test_kelly_paper_order_sync_cli.py tests/test_kelly_paper_order_sync.py tests/test_kelly_lab.py tests/test_dashboard.py tests/test_dashboard_web.py -q
```

- [ ] **Step 2: Run Kelly UI Playwright**

```bash
npm run test:e2e:kelly
```

## Self-Review

- The command is fake-only and cannot reach real trading.
- The command does not modify existing daily premarket or trade-action behavior.
- The implementation respects the existing dirty worktree by keeping changes scoped to Kelly sync files and `cli.py`.
