# Kelly Order Risk Checks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a pre-execution risk-check artifact for Kelly order intents, without placing Futu orders.

**Architecture:** Read `data/latest/kelly_order_intents.json`, evaluate each intent, and write `data/latest/kelly_order_risk_checks.json`. First version only gates entry/buy intents; exit/sell intents are approved because they reduce exposure.

**Tech Stack:** Python JSON artifacts, argparse CLI, pytest, existing Kelly Lab Playwright verification.

---

## Files

- Create: `src/open_trader/kelly_order_risk.py`
  - Add `ORDER_RISK_CHECKS_SCHEMA_VERSION`.
  - Add `load_kelly_order_intents(data_dir)`.
  - Add `build_kelly_order_risk_checks_payload(intent_payload, checked_at=None, max_entry_position_pct="4")`.
  - Add `build_kelly_order_risk_checks(data_dir, checked_at=None, max_entry_position_pct="4")`.
  - Add `write_kelly_order_risk_checks(data_dir, payload)`.
- Modify: `src/open_trader/cli.py`
  - Add `open-trader kelly check-order-risk`.
- Create: `tests/test_kelly_order_risk.py`
  - Cover approved entry, blocked entry, default-approved exit, artifact writing.
- Create: `tests/test_kelly_order_risk_cli.py`
  - Cover CLI parser and wiring.

## Rules

- `intent_type="exit"` or `side="sell"` is approved with `risk_status="approved"` and `execution_status="ready"`.
- `intent_type="entry"` or `side="buy"` must have a valid positive `per_symbol_budget`.
- `entry` must have a valid positive `suggested_position_pct`.
- `entry` is blocked if `suggested_position_pct` is greater than `max_entry_position_pct`; the default is `4`.
- Approved entries include `planned_notional = per_symbol_budget * suggested_position_pct / 100`.
- Blocked entries include `risk_status="blocked"` and `execution_status="risk_blocked"`.
- No Futu API call is made.

## Verification

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_order_risk.py tests/test_kelly_order_risk_cli.py tests/test_kelly_order_intents.py tests/test_kelly_order_intents_cli.py tests/test_kelly_paper_order_sync_cli.py tests/test_kelly_paper_order_sync.py tests/test_kelly_lab.py tests/test_dashboard.py tests/test_dashboard_web.py -q
.venv/bin/python -m py_compile src/open_trader/kelly_order_risk.py src/open_trader/cli.py
.venv/bin/python -m open_trader kelly build-order-intents --data-dir data --created-at '2026-07-10 13:30'
.venv/bin/python -m open_trader kelly check-order-risk --data-dir data --checked-at '2026-07-10 13:31'
npm run test:e2e:kelly
```
