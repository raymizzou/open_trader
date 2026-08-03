# Account v1 Contract And Sync Worker Rename Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Freeze the Account v1 read contract and rename the sole account/quote publisher from Account Sync Controller to Account Sync Worker without changing its persisted data or runtime behavior.

**Architecture:** R1 remains file-backed and does not start the future Account API. A new contract document fixes the future `GET /api/v1/account/snapshot` envelope, ownership, generations, IDs, freshness, and error semantics; the existing publisher is then renamed mechanically at the Python and operator interfaces while its launchd label and persisted artifact identifiers remain stable.

**Tech Stack:** Python 3.12, argparse, pytest, Bash, macOS launchd, Markdown.

## Global Constraints

- Account owns broker accounts, positions, cash, quotes, sync status, and statement-publication facts only.
- Trend owns strategy risk, actions, candidates, discipline, and decision plans; Research owns research conclusions, research facts, and standard backtests.
- The sole v1 read route is `GET /api/v1/account/snapshot`; R1 does not open port `8768` or route traffic.
- v1 may only add optional fields; removal, rename, or semantic change requires v2.
- Preserve the current JSON/CSV persistence schema, `data/account_sync/controller_status.json`, `data/account_sync/controller.lock`, and `open_trader.account_sync.controller.v1`.
- Preserve the actual launchd identity `com.open-trader.account-sync-controller` and its template filename; only its program command changes to `account-sync-worker`.
- Do not add a compatibility import, class alias, CLI alias, database, queue, cache service, or third-party dependency.
- Do not change Dashboard layout, browser behavior, strategy, reports, statement import, quote cadence, or execution behavior.
- Add the dated operator-facing `CHANGELOG.md` entry before merging.

---

### Task 1: Freeze The Account v1 Public Contract

**Files:**
- Create: `docs/superpowers/specs/2026-08-03-account-v1-contract.md`

**Interfaces:**
- Consumes: Issue #19 decisions and the existing `dashboard_projection` fields in `src/open_trader/account_sync_state.py`.
- Produces: The exact future `GET /api/v1/account/snapshot` response contract used by R2 and later consumers.

- [ ] **Step 1: Write the contract document**

Define this exact top-level success shape:

```json
{
  "schema_version": 1,
  "snapshot_generation": "sha256:<64 lowercase hex characters>",
  "account_generation": "sha256:<64 lowercase hex characters>",
  "generated_at": "2026-08-03T12:00:05+08:00",
  "quote_as_of": "2026-08-03T12:00:04+08:00",
  "status": "healthy",
  "stale": false,
  "sources": {
    "account": {
      "status": "healthy",
      "as_of": "2026-08-03T12:00:00+08:00",
      "reason": null,
      "brokers": {}
    },
    "quotes": {"status": "healthy", "as_of": "2026-08-03T12:00:04+08:00", "reason": null}
  },
  "release": {"api_git_sha": "<40 lowercase hex characters>", "worker_git_sha": "<40 lowercase hex characters>"},
  "summary": {},
  "broker_summaries": [],
  "positions": [],
  "cash_balances": [],
  "errors": []
}
```

Define the existing Account-owned summary, broker summary, position, and cash fields exactly, adding only `instrument_id` and `position_id` to position rows. Define decimal money/quantity fields as JSON strings so the API does not introduce float rounding.

- [ ] **Step 2: Define deterministic opaque IDs**

Specify UTF-8 JSON arrays with no insignificant whitespace as the canonical input:

```text
instrument_id = "ins_" + sha256(json([upper(trim(market)), lower(trim(asset_class)), upper(trim(symbol))])).hexdigest()
position_id = "pos_" + sha256(json([lower(trim(broker)), trim(account_alias), instrument_id])).hexdigest()
```

State that consumers compare IDs only and must not parse them for market, broker, account, or symbol semantics.

- [ ] **Step 3: Define generation and ETag semantics**

State that `account_generation` hashes the accepted Account facts only and is unchanged by a failed refresh that preserves those facts. State that `snapshot_generation` hashes the canonical success payload excluding `snapshot_generation` itself, so any representation change changes the generation. Define the response header as:

```text
ETag: "account-v1-<snapshot_generation hex without sha256: prefix>"
```

Define `If-None-Match` equality as `304` with no body.

- [ ] **Step 4: Define freshness, release, and failure semantics**

Specify `healthy` and `stale` as the only valid `200` statuses; top-level status is the worse of Account and quotes. A failed refresh with a last valid publication is `200 stale`; missing, malformed, unsupported-schema, or no-last-valid publication is `503`. Closed-market last valid quotes are not stale merely because time passes; quote health follows the scheduled refresh result and missing/invalid quote coverage. If Account API and Worker SHAs differ, snapshot returns `503 account_release_mismatch`, while `/healthz` remains a `200` liveness response that reports both SHAs.

Use this exact error item shape in both stale `200` responses and `503` envelopes:

```json
{"code": "quotes_refresh_failed", "source": "quotes", "message": "sanitized operator-safe text", "retryable": true}
```

- [ ] **Step 5: Define ownership exclusions and evolution**

Explicitly forbid `risk_flag`, Trend/Research enrichment, action recommendations, and a global `actionable` field. State that v1 accepts additive optional fields only and that a breaking change publishes `/api/v2/account/snapshot`.

- [ ] **Step 6: Validate the contract is complete**

Run:

```bash
rg -n "GET /api/v1/account/snapshot|schema_version|snapshot_generation|account_generation|quote_as_of|instrument_id|position_id|200.*stale|503|account_release_mismatch|risk_flag|actionable|v2" docs/superpowers/specs/2026-08-03-account-v1-contract.md
```

Expected: every required contract term appears in a normative section, with no implementation placeholders.

- [ ] **Step 7: Commit**

```bash
git add docs/superpowers/specs/2026-08-03-account-v1-contract.md docs/superpowers/plans/2026-08-03-account-v1-contract-worker-rename.md
git commit -m "docs: freeze Account v1 contract (#19)"
```

### Task 2: Rename The Publisher Atomically To Account Sync Worker

**Files:**
- Move: `src/open_trader/account_sync_controller.py` to `src/open_trader/account_sync_worker.py`
- Move: `tests/test_account_sync_controller.py` to `tests/test_account_sync_worker.py`
- Modify: `src/open_trader/cli.py`
- Modify: `src/open_trader/daily_premarket.py`
- Modify: `src/open_trader/t_signal_runner.py`
- Modify: `src/open_trader/dashboard_acceptance.py`
- Modify: `ops/launchd/com.open-trader.account-sync-controller.plist.template`
- Modify: `scripts/install_account_sync_launchd.sh`
- Modify: `tests/test_account_sync_cli.py`
- Modify: `tests/test_account_sync_launchd.py`
- Modify: `tests/test_dashboard_web.py`
- Modify: `tests/test_dashboard_acceptance.py`
- Modify: `tests/test_t_signal_runner.py`

**Interfaces:**
- Consumes: Existing account/quote sync implementation and persistence contracts unchanged.
- Produces: `AccountSyncWorkerConfig`, `AccountSyncWorker`, `run_account_sync_worker(config, *, once=False, clock=time.monotonic, sleep_fn=time.sleep)`, and CLI command `account-sync-worker`.

- [ ] **Step 1: Write the failing rename tests**

Change tests to import:

```python
from open_trader.account_sync_worker import (
    AccountSyncWorker,
    AccountSyncWorkerConfig,
    run_account_sync_worker,
)
```

Change CLI and plist expectations to `account-sync-worker`, while retaining:

```python
assert payload["Label"] == "com.open-trader.account-sync-controller"
assert TEMPLATE.name == "com.open-trader.account-sync-controller.plist.template"
```

Add assertions that the old module and CLI command do not exist:

```python
with pytest.raises(ModuleNotFoundError):
    importlib.import_module("open_trader.account_sync_controller")
with pytest.raises(SystemExit):
    build_parser().parse_args(["account-sync-controller"])
```

- [ ] **Step 2: Run tests to verify the rename is red**

Run:

```bash
.venv/bin/python -m pytest tests/test_account_sync_worker.py tests/test_account_sync_cli.py tests/test_account_sync_launchd.py -q
```

Expected: collection fails because `open_trader.account_sync_worker` does not exist.

- [ ] **Step 3: Move the module and rename its public symbols**

Apply these exact symbol replacements without changing loop, lock, publication,
broker, quote, cadence, or error-handling method bodies:

```text
AccountSyncControllerConfig -> AccountSyncWorkerConfig
AccountSyncController       -> AccountSyncWorker
run_account_sync_controller -> run_account_sync_worker
controller local variables  -> worker
```

Keep the existing function parameters and defaults unchanged:

```text
config: AccountSyncWorkerConfig
once: bool = False
clock: Callable[[], float] = time.monotonic
sleep_fn: Callable[[float], None] = time.sleep
return type: int
```

Keep the existing lock path, heartbeat path, and heartbeat schema version verbatim. Change only the duplicate-lock message to `已有账户同步 Worker 运行`.

- [ ] **Step 4: Rename the CLI and launchd program command**

Make `build_parser()` expose `account-sync-worker` and dispatch it through `run_account_sync_worker`. Change the plist `ProgramArguments` command to `account-sync-worker`; keep its label and filename unchanged. Rename the launchd readiness helper to `worker_status_matches` and its operator error to `account sync worker did not publish a matching fresh status`.

- [ ] **Step 5: Rename active operator-facing role references**

Change current runtime errors and acceptance helper/copy to `Account Sync Worker` / `账户同步 Worker`. Keep persisted mapping keys named `controller`, status/lock paths, schema strings, and historical specs/plans unchanged because they are existing compatibility or history, not the public role name.

- [ ] **Step 6: Run the focused rename suites**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_account_sync_worker.py \
  tests/test_account_sync_state.py \
  tests/test_account_sync_cli.py \
  tests/test_account_sync_launchd.py \
  tests/test_daily_premarket.py \
  tests/test_t_signal_runner.py \
  tests/test_dashboard_acceptance.py \
  tests/test_dashboard_web.py -q
```

Expected: all selected tests pass.

- [ ] **Step 7: Verify old runtime entry points are absent and compatibility IDs remain**

Run:

```bash
test ! -e src/open_trader/account_sync_controller.py
test ! -e tests/test_account_sync_controller.py
! PYTHONPATH=src .venv/bin/python -m open_trader account-sync-controller --once
PYTHONPATH=src .venv/bin/python -m open_trader account-sync-worker --help
rg -n "com\.open-trader\.account-sync-controller|open_trader\.account_sync\.controller\.v1|controller_status\.json|controller\.lock" src tests scripts ops
```

Expected: the old Python/CLI entry points fail, the Worker help succeeds, and compatibility identifiers remain covered.

- [ ] **Step 8: Commit**

```bash
git add src/open_trader tests scripts ops/launchd/com.open-trader.account-sync-controller.plist.template
git commit -m "refactor: rename account sync worker (#19)"
```

### Task 3: Update Operator Docs And Prove The Real Worker Cutover

**Files:**
- Modify: `README.md`
- Modify: `docs/monthly_portfolio_import.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: The renamed CLI and the unchanged launchd/persistence identifiers from Task 2.
- Produces: Current operator instructions, merge-gate log entry, and live runtime evidence from the exact candidate SHA.

- [ ] **Step 1: Update current operator documentation**

Rename the role and command in `README.md` and `docs/monthly_portfolio_import.md`. Add a short compatibility note beside the install instructions:

```text
The stable launchd label and persisted status/lock names retain the historical
`controller` token during R1; operators run `account-sync-worker`.
```

Do not rewrite historical specs, plans, or old changelog entries.

- [ ] **Step 2: Add the dated changelog entry**

Under `## 2026-08-03`, add an operator-facing bullet that says the publisher is now Account Sync Worker, the command is `account-sync-worker`, the existing launchd label/persisted files remain unchanged, and no Account API or traffic cutover occurred.

- [ ] **Step 3: Run documentation and source invariants**

Run:

```bash
rg -n "account-sync-worker|Account Sync Worker|账户同步 Worker" README.md docs/monthly_portfolio_import.md src/open_trader tests scripts ops
rg -n "account-sync-controller|Account Sync Controller|account sync controller|账户同步控制器" README.md docs/monthly_portfolio_import.md src/open_trader tests scripts ops
```

Expected: the second command reports only the explicitly preserved launchd label/template path, heartbeat/lock compatibility identifiers, and tests asserting those identifiers; it reports no old Python module, class, CLI command, or active operator-facing role copy.

- [ ] **Step 4: Run full automated verification**

Run:

```bash
.venv/bin/python -m pytest -q
```

Expected: all tests pass with zero failures.

- [ ] **Step 5: Run direct CLI and launchd checks**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m open_trader account-sync-worker --help
scripts/install_account_sync_launchd.sh \
  --repo-root "$PWD" \
  --runtime-root /Users/ray/projects/open_trader
launchctl print "gui/$UID/com.open-trader.account-sync-controller"
PYTHONPATH=src .venv/bin/python -m open_trader account-sync-status \
  --data-dir /Users/ray/projects/open_trader/data --json
```

Expected: launchd reports one live PID whose `ProgramArguments` use `account-sync-worker`; `controller_status.json` reports that PID, this worktree, the candidate SHA, and a fresh heartbeat; stderr contains no fresh traceback or old-command failure.

- [ ] **Step 6: Commit the merge-gate docs**

```bash
git add README.md docs/monthly_portfolio_import.md CHANGELOG.md
git commit -m "docs: publish Account Sync Worker runbook (#19)"
```

- [ ] **Step 7: Merge locally and redeploy the exact accepted SHA**

From the root checkout, preserve unrelated dirty files and run:

```bash
git merge --ff-only codex/issue-19-account-contract-worker
```

Then rerun the full pytest suite on local `main`, reinstall the Worker from the clean issue worktree at the same merged SHA, and verify PID, working directory, Git SHA, fresh heartbeat/logs, and `account-sync-status` again. Do not run `make acceptance` because R1 changes no browser behavior.

- [ ] **Step 8: Publish evidence and stop at the operator gate**

Post the contract path, commit SHA, focused/full test counts, direct CLI result, live Worker PID/cwd/SHA/heartbeat/log status, and the unchanged launchd label to Issue #19. Leave Issue #20 without `ready-for-agent` until the operator confirms this R1 review.
