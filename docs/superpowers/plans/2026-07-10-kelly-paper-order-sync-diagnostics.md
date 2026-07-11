# Kelly Paper Order Sync Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a diagnostic report for Kelly paper-order sync so skipped Futu simulate orders have explicit reasons.

**Architecture:** Keep order artifact writing unchanged. Extend the Futu simulate client to collect match diagnostics while normalizing Futu order rows, then optionally write `data/latest/kelly_paper_order_sync_report.json` from the CLI when `--diagnose` is set.

**Tech Stack:** Python JSON artifacts, argparse, pytest fakes, existing Kelly Lab Playwright verification.

---

## Files

- Modify: `src/open_trader/kelly_paper_order_sync.py`
  - Add `PAPER_ORDER_SYNC_REPORT_SCHEMA_VERSION`.
  - Add symbol-index details that preserve ambiguous strategy matches.
  - Add `build_kelly_paper_order_sync_report()` and `write_kelly_paper_order_sync_report()`.
  - Record matched and skipped Futu order diagnostics.
- Modify: `src/open_trader/cli.py`
  - Add `--diagnose`.
  - Print matched/skipped counts when diagnostics are enabled.
  - Write `data/latest/kelly_paper_order_sync_report.json`.
- Modify: `tests/test_kelly_paper_order_sync.py`
  - Cover matched, untracked, ambiguous, and invalid-code diagnostics.
- Modify: `tests/test_kelly_paper_order_sync_cli.py`
  - Cover CLI diagnostic report wiring.

## Behavior

- `--diagnose` is optional and works with both `--fake` and `--futu-simulate`.
- Futu orders with exactly one strategy participant match are written to `kelly_paper_orders.json`.
- Futu orders with no participant match are skipped as `untracked_symbol`.
- Futu orders where the symbol appears under multiple experiments are skipped as `ambiguous_symbol`.
- Futu rows without a parseable `MARKET.SYMBOL` code are skipped as `invalid_code`.

## Verification

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_paper_order_sync_cli.py tests/test_kelly_paper_order_sync.py tests/test_kelly_lab.py tests/test_dashboard.py tests/test_dashboard_web.py -q
npm run test:e2e:kelly
```

## Self-Review

- The report is diagnostic only; it does not change order placement or strategy lifecycle.
- Existing `kelly_paper_orders.json` schema remains unchanged.
- Ambiguous strategy attribution remains conservative: skipped instead of guessed.
