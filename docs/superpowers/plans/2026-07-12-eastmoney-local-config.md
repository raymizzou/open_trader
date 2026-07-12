# Eastmoney Local Statement Config Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Read the encrypted Eastmoney statement path and password from the existing ignored local env file, import the real statement securely, and pass Dashboard acceptance.

**Architecture:** Extend `import-statements` with the same local env-file contract used by daily premarket configuration. Keep explicit CLI paths authoritative, keep `getpass` as a fallback, and never expose the password. Store the PDF under ignored runtime data and use the existing import pipeline unchanged after resolving inputs.

**Tech Stack:** Python 3, argparse, pytest, local env files, existing statement pipeline, Dashboard acceptance gate.

## Global Constraints

- Local config path is `config/daily_premarket.env`, mode `0600`, ignored by Git.
- Statement path is `data/statements/eastmoney/2026-07/statement.pdf`, ignored by Git.
- Config keys are `OPEN_TRADER_EASTMONEY_STATEMENT` and `OPEN_TRADER_EASTMONEY_PDF_PASSWORD`.
- Explicit `--eastmoney` overrides the configured path.
- Missing password falls back to hidden `getpass`.
- Password never appears in arguments, logs, exceptions, generated files, or tracked examples.
- Phillips-only and combined import behavior remains compatible.
- Deployment is complete only when `make acceptance` reports `PASS`.

---

### Task 1: Config-Aware Eastmoney Import CLI

**Files:**
- Modify: `src/open_trader/cli.py`
- Modify: `config/daily_premarket.env.example`
- Modify: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `--config Path`, explicit `--eastmoney Path`, and the two local config keys.
- Produces: resolved Eastmoney statement path and password passed to `EastmoneyStatementParser` without exposing the password.

- [ ] **Step 1: Write failing tests**

Add tests proving:

```python
def test_cli_imports_eastmoney_path_and_password_from_local_config():
    # Config contains path/password; no --eastmoney argument and no getpass call.
    # Assert parser receives the password and run_import receives the configured path.


def test_cli_explicit_eastmoney_path_overrides_config():
    # Config supplies password and old path; --eastmoney supplies new path.


def test_cli_prompts_when_config_password_is_blank():
    # Assert hidden getpass fallback remains.


def test_cli_rejects_missing_configured_statement_without_leaking_password():
    # Error names the missing path and does not contain the secret.
```

Also assert `config/daily_premarket.env.example` contains empty placeholders only.

- [ ] **Step 2: Run RED tests**

```bash
.venv/bin/python -m pytest tests/test_pipeline.py -q
```

Expected: new tests fail because `import-statements` does not yet read `--config`.

- [ ] **Step 3: Implement minimal config resolution**

Add `--config` with default `Path("config/daily_premarket.env")`. Reuse the
existing env-file parser contract, returning an empty mapping when the optional
file does not exist. Resolve:

```python
eastmoney_path = args.eastmoney or _optional_path(
    config_values.get("OPEN_TRADER_EASTMONEY_STATEMENT")
)
eastmoney_password = (
    config_values.get("OPEN_TRADER_EASTMONEY_PDF_PASSWORD", "").strip()
    or getpass("东方财富对账单密码: ")
)
```

Validate the resolved path before constructing the parser. Never include the
password in an error or output. Add empty keys to the tracked example.

- [ ] **Step 4: Run GREEN and regression tests**

```bash
.venv/bin/python -m pytest tests/test_pipeline.py tests/test_eastmoney_parser.py tests/test_daily_premarket.py -q
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_trader/cli.py config/daily_premarket.env.example tests/test_pipeline.py
git commit -m "feat: load eastmoney statement credentials from local config"
```

### Task 2: Secure Local Statement Migration and Real Import

**Files:**
- Runtime only: `data/statements/eastmoney/2026-07/statement.pdf`
- Runtime only: `config/daily_premarket.env`
- Runtime output: `data/runs/2026-07/`, `data/latest/portfolio.csv`

**Interfaces:**
- Consumes: user-populated local password key.
- Produces: 5 Eastmoney CN holdings plus CNY cash merged into the current portfolio.

- [ ] **Step 1: Move the PDF without changing contents**

Move `/Users/ray/Downloads/电子对账单.pdf` to the target path. Compare SHA-256
before and after. Confirm Git ignores the destination and local env remains mode
`0600`.

- [ ] **Step 2: Add non-secret local config entries**

Append the absolute statement path and an empty password key only if absent.
Stop and ask the user to fill the password locally; never read or display the
configured value.

- [ ] **Step 3: Run the real import after the user confirms configuration**

```bash
.venv/bin/python -m open_trader import-statements \
  --month 2026-07 \
  --config config/daily_premarket.env \
  --cny-hkd 1.1549 \
  --fx-date 2026-06-30 \
  --data-dir data \
  --update-latest
```

Expected: 5 CN positions, one CNY cash row, and no secret output.

- [ ] **Step 4: Validate imported runtime data**

Use CSV-aware checks to confirm 33 total rows, 5 non-cash CN/eastmoney rows,
one CNY cash row, and exact Eastmoney CNY total `676549.55`.

### Task 3: Full Verification and Deployment

**Files:**
- No planned source changes.

**Interfaces:**
- Consumes: merged source, updated dependencies, and imported runtime data.
- Produces: accepted live Dashboard on port 8766.

- [ ] **Step 1: Run automated verification**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q
npm run test:e2e:kelly
.venv/bin/python -m compileall -q src tests
git diff --check
```

- [ ] **Step 2: Restart Dashboard from main**

Stop the existing screen and any surviving Python child, then start
`open_trader_dashboard_8766` from `/Users/ray/projects/open_trader`. Record the
new PID, start time, cwd, and fresh log.

- [ ] **Step 3: Run the required acceptance gate**

```bash
make acceptance
```

Expected: final JSON status is `PASS`. `FAIL` must be fixed; `BLOCKED` must be
reported without substituting partial checks.

- [ ] **Step 4: Verify final repository state**

Confirm `main` is clean, local secrets/PDF remain ignored, and no password is
present in tracked diffs or Git history.
