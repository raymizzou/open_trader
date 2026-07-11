# Kelly Order Intents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a pre-execution order intent artifact for Kelly strategies, without calling Futu order placement.

**Architecture:** Build intents from Kelly experiments whose lifecycle states are `pending_entry_order` or `pending_exit_order`. Persist them to `data/latest/kelly_order_intents.json`; later execution code will read this artifact, place simulate orders, and write `kelly_order_links.json`.

**Tech Stack:** Python JSON artifacts, argparse CLI, pytest, existing Kelly Lab Playwright verification.

---

## Files

- Create: `src/open_trader/kelly_order_intents.py`
  - Add `ORDER_INTENTS_SCHEMA_VERSION`.
  - Add `build_kelly_order_intents_payload(experiments, created_at=None)`.
  - Add `build_kelly_order_intents(data_dir, created_at=None)`.
  - Add `write_kelly_order_intents(data_dir, payload)`.
- Modify: `src/open_trader/cli.py`
  - Add `open-trader kelly build-order-intents`.
- Create: `tests/test_kelly_order_intents.py`
  - Cover entry and exit intent generation.
  - Cover artifact writing.
- Create: `tests/test_kelly_order_intents_cli.py`
  - Cover CLI parser and wiring.

## Behavior

- Only running experiments produce intents.
- `pending_entry_order` becomes side `buy`, intent type `entry`.
- `pending_exit_order` becomes side `sell`, intent type `exit`.
- Intent IDs are deterministic: `experiment_id:market:symbol:intent_type`.
- Generated intents have `execution_status="pending"` and `risk_status="not_checked"`.
- No Futu API call is made.

## Verification

Run:

```bash
.venv/bin/python -m pytest tests/test_kelly_order_intents.py tests/test_kelly_order_intents_cli.py tests/test_kelly_paper_order_sync_cli.py tests/test_kelly_paper_order_sync.py tests/test_kelly_lab.py tests/test_dashboard.py tests/test_dashboard_web.py -q
npm run test:e2e:kelly
```
