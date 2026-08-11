# Prediction Control Mutations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the four frozen Prediction control mutations work on a production-owned 8769 service without publishing or routing that service yet.

**Architecture:** Extend the existing store, execution service, runtime, and HTTP server in place. Production mode acquires the existing runtime ownership lock, completes startup reconciliation, and only then binds 8769; Shadow remains read-only. A final downtime ticket will later stop Legacy and switch the whole API prefix, so this ticket contains no partial routing, shared sessions, launchd production job, or `NOT_READY` recovery API.

**Tech Stack:** Python 3.12 stdlib (`http.server`, `sqlite3`, `threading`, `hashlib`, `secrets`), existing `PredictionRuntime`, `PredictionExecutionService`, `PredictionArbitrageStore`, and pytest.

## Global Constraints

- Keep the frozen paths and request shapes exactly: `/mode {mode}`, `/circuit-breaker/reset {incident_id}`, `/predict-allowance/cleanup {confirm:true}`, and `/cross-auto/pause {confirm:true}`.
- Keep the old `observe_only < manual < auto` mode only; do not build `YES_NO` / `LLM_RELATION` / `N_LEG` mode storage in this ticket.
- Do not change Gateway, Legacy handlers, Dashboard UI, production launchd state, preview, execution, or CLI re-arm behavior.
- Production 8769 must not bind unless Runtime is `RUNNING` after acquiring the production owner lock and completing startup reconciliation.
- Shadow mode must continue rejecting every Prediction mutation before reading the body or touching downstream objects.
- Production POST authorization remains loopback plus exact Host/Origin, `ot_prediction_session`, `X-CSRF-Token`, strict JSON schema, and a 1 MiB body cap.
- Session and CSRF values are generated per process; no Legacy sharing or persistence.
- Breaker-open or active-execution behavior follows risk direction: downgrades and pause remain available; upgrades, reset, and cleanup use fail-closed gates and the existing execution lock.
- Use natural idempotence; do not add request fields or idempotency keys to the four frozen control paths.
- Validation uses only temporary data and fake trading clients. Do not touch production SQLite, wallets, allowance, launchd, Gateway, or Legacy processes.

---

### Task 1: Durable control audit and safety policy

**Files:**
- Modify: `src/open_trader/prediction_arbitrage_store.py`
- Modify: `tests/test_prediction_arbitrage_store.py`

**Interfaces:**
- Produces: `PredictionArbitrageStore.apply_safety_policy(policy, *, git_sha) -> dict[str, object]`
- Produces: `PredictionArbitrageStore.safety_policy() -> dict[str, object] | None`
- Extends: `set_validation_mode(mode, *, audit=None)` and `pause_cross_auto(reason, *, audit=None)` without breaking existing callers.
- Produces: `begin_control_event(...)`, `finish_control_event(...)`, and `latest_control_event(action, target)` for external maintenance operations.

- [ ] **Step 1: Write failing store tests for baseline enrollment and downgrade**

  Add literal tests proving that the first valid legacy database preserves `auto` / armed cross-auto while storing `baseline_enrolled`, an identical policy is a no-op, and a changed policy atomically changes `auto -> manual` plus `auto_submit/armed -> manual_confirm/unarmed` with a `safety_policy_changed` audit event. The production mutation that each test catches is an accidental first-start downgrade, a restart-only downgrade, or a missed safety downgrade.

- [ ] **Step 2: Run the policy tests and verify RED**

  Run:
  ```bash
  PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
    tests/test_prediction_arbitrage_store.py -k 'safety_policy or control_event'
  ```
  Expected: failures because the policy and audit interfaces do not exist.

- [ ] **Step 3: Add the minimal SQLite schema and policy transaction**

  Add only two tables: a singleton `safety_policy` row and append-only `control_events` rows. Store audit details as sanitized JSON; keep action, target, outcome, and timestamps queryable. `apply_safety_policy` must perform policy update, mode downgrade, cross-auto downgrade, and audit insertion in one `BEGIN IMMEDIATE` transaction.

  Canonical policy encoding must use sorted compact JSON and SHA-256:
  ```python
  encoded = json.dumps(policy, sort_keys=True, separators=(",", ":"))
  fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
  ```

- [ ] **Step 4: Write failing tests for natural idempotence and audit atomicity**

  Prove that setting the current mode and pausing an already-paused state return the current successful shape, append `no_op`, and do not create a second state transition. Inject a SQLite failure and prove neither the local state nor a success audit is committed.

- [ ] **Step 5: Implement audited local state writes and external event lifecycle**

  Reuse the existing transactions. Optional `audit=None` preserves current Legacy/CLI behavior; an audit mapping causes state and event to commit together. `begin_control_event` records `started`; `finish_control_event` updates exactly one event to `succeeded`, `rejected`, or `failed`. Reject terminal-to-terminal rewrites.

- [ ] **Step 6: Run the store suite GREEN and commit**

  Run the focused selector, then:
  ```bash
  PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
    tests/test_prediction_arbitrage_store.py
  git diff --check
  ```
  Commit: `feat: persist prediction control policy and audit`

### Task 2: Runtime policy identity and fail-closed production startup

**Files:**
- Modify: `src/open_trader/prediction_runtime.py`
- Modify: `tests/test_prediction_runtime.py`

**Interfaces:**
- Produces: read-only `PredictionRuntime.mode` and `PredictionRuntime.production_owner` properties.
- Extends: `PredictionRuntime(..., git_sha: str = "")`; the service passes the SHA it already resolved for health metadata.
- Consumes: `PredictionArbitrageStore.apply_safety_policy(...)` from Task 1.
- Produces: `_prediction_safety_policy(trading_config) -> dict[str, object]` containing no credentials.

- [ ] **Step 1: Write failing runtime tests**

  Cover: Shadow never enrolls a production policy; production enrolls before startup reconciliation; legal first enrollment preserves persisted modes; changed semantic policy downgrades before reconciliation; a locked or raised reconciliation leaves Runtime `NOT_READY`; ownership is true only while the production lock is held.

- [ ] **Step 2: Run runtime tests and verify RED**

  Run:
  ```bash
  PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
    tests/test_prediction_runtime.py -k 'policy or production_owner or reconcile'
  ```
  Expected: missing policy/properties and ordering assertions fail.

- [ ] **Step 3: Build the semantic safety material with stdlib only**

  Include the explicit policy version, wallet/signer/Predict environment identities, `MAX_NORMAL_COST`, cross unsettled/daily caps, `MAX_EMERGENCY_LOSS`, minimum profit/yield, and book freshness. Use normalized strings only; do not include private keys, tokens, raw config text, or Git SHA in the fingerprint. Pass the constructor's Git SHA separately for audit evidence.

- [ ] **Step 4: Enroll/downgrade before `reconcile_startup()`**

  After opening the store and loading validated trading config, apply the policy before monitors start and before startup reconciliation. A policy persistence error follows the existing startup construction failure path and releases the ownership lock. Do not add a recovery state machine.

- [ ] **Step 5: Run runtime regressions GREEN and commit**

  Run:
  ```bash
  PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
    tests/test_prediction_runtime.py tests/test_prediction_arbitrage_store.py
  git diff --check
  ```
  Commit: `feat: gate prediction startup on safety policy`

### Task 3: Safe and idempotent execution controls

**Files:**
- Modify: `src/open_trader/prediction_arbitrage_execution.py`
- Modify: `tests/test_prediction_arbitrage_execution.py`

**Interfaces:**
- Extends the existing four methods with optional keyword-only `audit: Mapping[str, object] | None = None`.
- Returns `{"state": "busy", "reason": "control_in_progress"}` when a maintenance operation cannot acquire the existing execution lock.
- Keeps every existing successful response body unchanged.

- [ ] **Step 1: Write failing risk-direction and conflict tests**

  Prove: breaker-open `auto -> manual/observe_only` is allowed; `observe_only -> manual` and any `-> auto` are locked; pause works during an active execution; upgrades, reset, and cleanup cannot race an active execution or another maintenance call.

- [ ] **Step 2: Run the focused tests and verify RED**

  Run:
  ```bash
  PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
    tests/test_prediction_arbitrage_execution.py -k 'control or validation_mode or reset_breaker or cleanup_predict_allowance or pause_cross_auto'
  ```
  Expected: risk-direction, lock, and audit assertions fail against current direct calls.

- [ ] **Step 3: Reuse the current lock and store writes**

  Add no new lock type. Mode downgrades and pause use SQLite atomic writes immediately. Mode upgrades, breaker reset, and allowance cleanup acquire the existing execution file lock without waiting; release it in `finally`. Keep the existing fresh account, incident, allowance, gas, and reconciliation checks unchanged.

- [ ] **Step 4: Write failing duplicate maintenance tests**

  Prove an acknowledged incident returns its prior reset success without a second acknowledgement, an already-zero allowance returns the successful cleanup shape without a chain call, and a persisted `cleanup started` event is reconciled from the fresh zero allowance after a simulated terminal-audit failure.

- [ ] **Step 5: Implement external started/terminal audit recovery**

  Persist `started` before the allowance network call. If that write fails, do not call the client. After a confirmed zero allowance, finish the event; if finishing fails, return `audit_persistence_failed`, keep the breaker open, and let the next call complete the stored event from fresh account state without submitting again.

- [ ] **Step 6: Run execution/store regressions GREEN and commit**

  Run:
  ```bash
  PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
    tests/test_prediction_arbitrage_execution.py tests/test_prediction_arbitrage_store.py
  git diff --check
  ```
  Commit: `feat: serialize prediction control mutations`

### Task 4: Production 8769 control HTTP contract

**Files:**
- Modify: `src/open_trader/prediction_service.py`
- Modify: `tests/test_prediction_service.py`
- Modify: `tests/test_prediction_api_contract.py`

**Interfaces:**
- Extends: `create_prediction_server(..., session_token=None, csrf_token=None, runtime_metadata=None)`.
- Extends: `serve_prediction_service(..., mode="shadow" | "production")`.
- Consumes: Runtime `mode`, `production_owner`, `state`, `store`, and `execution`.

- [ ] **Step 1: Add a frozen production control parity test and verify RED**

  Route the same literal four request/response cases used by Legacy through a production fake Runtime. Assert the state response sets `ot_prediction_session` with `SameSite=Strict; HttpOnly; Path=/` and exposes the CSRF token. Expected RED: server rejects non-shadow Runtime.

- [ ] **Step 2: Add security boundary RED tests**

  Cover wrong Host, malicious Origin, wrong/missing cookie, wrong/missing CSRF, invalid/missing/extra JSON fields, non-object JSON, body over 1 MiB, unsupported paths, and preview/execution returning `503 {"error":"prediction mutation is unavailable"}`. Assert authorization happens before body read and downstream dispatch.

- [ ] **Step 3: Implement the minimal production handler branch**

  Keep Shadow's early 403 branch unchanged. For production, require `state == RUNNING` and `production_owner is True` before creating the server. Generate per-process tokens with `secrets.token_urlsafe(32)` unless tests inject fixed values. Resolve runtime metadata once in `serve_prediction_service`, pass its Git SHA into Runtime and the same mapping into the server, and dispatch only the four paths to existing execution methods with sanitized audit context. Map invalid input to 400, auth to 403, body size to 413, control conflict to 409, SQLite/unavailable state to 503, and successful domain results to 200.

- [ ] **Step 4: Add and pass downtime lifecycle tests**

  Prove production `serve_prediction_service` starts Runtime before bind, never binds when startup ends `NOT_READY`/`FAILED` or lacks owner, and always stops Runtime/restores signals on bind failure or SIGTERM. Do not add a persistent production launchd plist.

- [ ] **Step 5: Run HTTP and contract regressions GREEN and commit**

  Run:
  ```bash
  PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
    tests/test_prediction_service.py tests/test_prediction_api_contract.py \
    tests/test_prediction_runtime.py
  git diff --check
  ```
  Commit: `feat: serve prediction control mutations on 8769`

### Task 5: Isolated direct verification and branch review

**Files:**
- Modify only if needed by a failing check: the Task 1-4 files.
- No production configuration, launchd, Gateway, Legacy, or Dashboard files.

**Interfaces:**
- Uses the production server factory with a temporary store and fake trading clients.

- [ ] **Step 1: Run the direct temporary workflow**

  Start 8769 on an ephemeral loopback port with a temporary data directory and fakes. GET state to obtain cookie/CSRF, exercise the four control paths including one duplicate, verify audit rows and preserved mode state after a restart, then terminate the process.

- [ ] **Step 2: Prove cleanup**

  Check the test PID exited and no listener remains on the chosen port. Confirm no production Prediction SQLite path, launchd label, wallet, allowance, Gateway, or Legacy process was touched.

- [ ] **Step 3: Run the relevant complete regression**

  Run:
  ```bash
  PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
    tests/test_prediction_service.py tests/test_prediction_api_contract.py \
    tests/test_prediction_runtime.py tests/test_prediction_arbitrage_store.py \
    tests/test_prediction_arbitrage_execution.py
  git diff --check
  ```

- [ ] **Step 4: Review the complete diff**

  Use the repository code-review workflow against merge-base `main`, fix every blocking Standards or Spec finding with a fresh RED/GREEN cycle, and rerun the relevant complete regression.

- [ ] **Step 5: Stop before merge**

  Report the branch SHA, exact test/direct-workflow evidence, and residual non-production scope. Do not update `CHANGELOG.md`, merge, deploy, or run Dashboard acceptance until the user explicitly requests merge or the final downtime release ticket begins.
