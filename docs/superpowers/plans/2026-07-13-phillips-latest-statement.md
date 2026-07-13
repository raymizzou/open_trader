# Phillips Latest Statement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Dashboard use and verify the latest archived Phillips statement.

**Architecture:** Reuse the existing Phillips parser/import pipeline. Add only statement-native FX and zero-position handling, then make acceptance derive Phillips expectations from the latest archived PDF instead of fixed row counts.

**Tech Stack:** Python, pdfplumber, pytest, existing Dashboard acceptance runner.

## Global Constraints

- Archive the supplied PDF under ignored `data/statements/phillips/`; do not commit it.
- Do not require a specific month; newest available statement wins.
- `make acceptance` must return PASS before review.

---

### Task 1: Parse statement-native values

**Files:**
- Modify: `src/open_trader/parsers/phillips.py`
- Test: `tests/test_parsers_text.py`

**Interfaces:**
- Consumes: Phillips Account Details and Securities Portfolio text.
- Produces: nonzero `Position` rows and `CashBalance.notes` containing `ref_fx=<rate>`.

- [x] Add a parser test containing a zero holding and statement HKD(Base); expect the zero holding to be absent and base cash to win.
- [x] Run that test and confirm it fails for the missing behavior.
- [x] Make the smallest parser change that passes it.
- [x] Run the parser tests and confirm they pass.

### Task 2: Verify and publish the latest real statement

**Files:**
- Modify: `src/open_trader/dashboard_acceptance.py`
- Test: `tests/test_dashboard_acceptance.py`
- Runtime data only: `/Users/ray/projects/open_trader/data/statements/phillips/2026-07-10/statement.pdf`

**Interfaces:**
- Consumes: latest Phillips manifest, archived PDF, and Dashboard payload.
- Produces: acceptance errors when source date or HKD total differs from the latest PDF.

- [x] Add a failing acceptance test proving fixed Phillips row counts are no longer required and an incorrect latest-statement total is rejected.
- [x] Implement the smallest independent latest-PDF comparison using the existing parser.
- [x] Move the supplied PDF into the ignored project data path and import it with `--update-latest`.
- [ ] Run focused tests, restart stale Dashboard processes, inspect fresh logs, then run `make acceptance` as the final gate.
