# Kelly Order Links Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `futu_order_id -> experiment_id` links so Kelly paper-order sync can attribute orders even when symbols are shared by multiple strategies.

**Architecture:** Add an optional `data/latest/kelly_order_links.json` artifact. The Futu simulate sync client accepts an order-link index and classifies Futu rows by `order_id` first, then falls back to the existing symbol-based attribution.

**Tech Stack:** Python JSON artifacts, pytest fakes, existing Kelly Lab Playwright verification.

---

## Files

- Modify: `src/open_trader/kelly_paper_order_sync.py`
  - Add `ORDER_LINKS_SCHEMA_VERSION`.
  - Add `load_kelly_order_links(data_dir)`.
  - Add `order_link_index` to `FutuSimulatePaperOrderClient`.
  - Prefer `order_id` attribution over symbol attribution.
- Modify: `src/open_trader/cli.py`
  - Load optional order links and pass them to `FutuSimulatePaperOrderClient`.
- Modify: `tests/test_kelly_paper_order_sync.py`
  - Cover optional missing links.
  - Cover valid link loading.
  - Cover linked orders overriding ambiguous-symbol fallback.
- Modify: `tests/test_kelly_paper_order_sync_cli.py`
  - Cover CLI passing links to the Futu client.

## Behavior

- Missing `kelly_order_links.json` means no links and no error.
- Existing sync behavior remains unchanged without links.
- If `futu_order_id` is linked, the order is attributed to that experiment even if the same symbol is ambiguous.
- Diagnostics mark these as `matched_by_order_link`.

## Verification

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_paper_order_sync_cli.py tests/test_kelly_paper_order_sync.py tests/test_kelly_lab.py tests/test_dashboard.py tests/test_dashboard_web.py -q
npm run test:e2e:kelly
```
