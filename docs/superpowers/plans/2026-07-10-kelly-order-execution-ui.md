# Kelly Order Execution UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show Kelly order execution dry-run/submission results in the Kelly Lab UI.

**Architecture:** Extend `load_kelly_lab_state()` to attach `data/latest/kelly_order_executions.json` records to matching experiments. Render a per-strategy "订单执行" section under "订单同步"; this is read-only UI and does not place orders.

**Tech Stack:** Python JSON artifact loading, existing dashboard state pipeline, vanilla JS static dashboard rendering, pytest, Playwright.

---

## Files

- Modify: `src/open_trader/kelly_lab.py`
  - Load optional `kelly_order_executions.json`.
  - Attach matching execution records under `experiment["order_execution"]`.
- Modify: `src/open_trader/dashboard_static/dashboard.js`
  - Render `order_execution` summary and execution table.
- Modify: `tests/test_dashboard.py`
  - Assert dashboard state exposes attached execution records.
- Modify: `tests/test_dashboard_web.py`
  - Assert Kelly Lab HTML includes execution summary, table rows, status labels, and skipped reasons.
- Modify: `tests/e2e/kelly-lab.spec.ts`
  - Assert browser UI shows execution results scoped to the active strategy tab.

## Behavior

- Missing `kelly_order_executions.json` is not an error.
- Invalid execution artifact does not break Kelly Lab; it attaches an `order_execution` object with failed status and a readable message.
- Only records with matching `experiment_id` are shown in that strategy tab.
- Status labels:
  - `dry_run` -> `预演`
  - `submitted` -> `已提交`
  - `skipped` -> `已跳过`
  - `failed` -> `执行失败`
- The UI shows symbol, side, price, quantity, planned notional, Futu order id, status, and error.

## Verification

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard.py tests/test_dashboard_web.py tests/test_kelly_lab.py -q
npm run test:e2e:kelly
```
