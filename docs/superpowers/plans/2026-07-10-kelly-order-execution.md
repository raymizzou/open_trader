# Kelly Order Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Kelly order execution step that turns approved risk checks into auditable dry-run or Futu simulate order submissions.

**Architecture:** Read `data/latest/kelly_order_risk_checks.json`, build order requests for checks with `risk_status="approved"` and `execution_status="ready"`, and write `data/latest/kelly_order_executions.json`. Dry-run is the default and never calls Futu; `--futu-simulate` uses a separate client that submits only to Futu SIMULATE accounts.

**Tech Stack:** Python JSON artifacts, argparse CLI, Futu OpenD through `futu-api`, pytest, existing Kelly Lab Playwright verification.

---

## Files

- Create: `src/open_trader/kelly_order_execution.py`
  - Add `ORDER_EXECUTIONS_SCHEMA_VERSION`.
  - Add `load_kelly_order_risk_checks(data_dir)`.
  - Add `execute_kelly_orders_from_risk_checks(...)`.
  - Add `write_kelly_order_executions(data_dir, payload)`.
  - Add `FutuSimulateOrderExecutionClient` for real simulate submissions.
- Modify: `src/open_trader/cli.py`
  - Add `open-trader kelly execute-orders`.
- Create: `tests/test_kelly_order_execution.py`
  - Cover dry-run buys, skipped sells without quantity, skipped blocked checks, fake client submission, and artifact writing.
- Create: `tests/test_kelly_order_execution_cli.py`
  - Cover CLI parser and wiring.

## Rules

- Only `risk_status="approved"` and `execution_status="ready"` can be submitted.
- Dry-run is default and produces `execution_status="dry_run"` with `submitted=false`.
- `--futu-simulate` submits with `trd_env=SIMULATE` only.
- Buy quantity is `floor(planned_notional / limit_price)`.
- Sell quantity must be provided with `--order-qty MARKET.SYMBOL=QTY`; missing sell quantity is skipped.
- Limit price must be provided with `--limit-price MARKET.SYMBOL=PRICE`; missing price is skipped.
- Futu account selection is explicit when using `--futu-simulate` if OpenD exposes multiple SIMULATE accounts.
- No REAL account order path is added.

## Verification

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_order_execution.py tests/test_kelly_order_execution_cli.py tests/test_kelly_order_risk.py tests/test_kelly_order_risk_cli.py tests/test_kelly_order_intents.py tests/test_kelly_order_intents_cli.py tests/test_kelly_paper_order_sync_cli.py tests/test_kelly_paper_order_sync.py tests/test_kelly_lab.py tests/test_dashboard.py tests/test_dashboard_web.py -q
.venv/bin/python -m py_compile src/open_trader/kelly_order_execution.py src/open_trader/cli.py
.venv/bin/python -m open_trader kelly build-order-intents --data-dir data --created-at '2026-07-10 13:30'
.venv/bin/python -m open_trader kelly check-order-risk --data-dir data --checked-at '2026-07-10 13:31'
.venv/bin/python -m open_trader kelly execute-orders --data-dir data --dry-run --executed-at '2026-07-10 13:32' --limit-price US.RAM=12.50 --limit-price HK.02840=3000
npm run test:e2e:kelly
```
