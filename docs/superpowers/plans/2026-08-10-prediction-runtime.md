# Prediction Runtime Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract the existing Legacy Prediction construction and lifecycle into a single, lock-owning `PredictionRuntime` without changing the #39 external contract or starting 8769.

**Architecture:** Add a Dashboard-independent `src/open_trader/prediction_runtime.py` that owns resource construction, lifecycle state, the data-directory-scoped advisory owner lock, cross-venue event-loop wrapper, and cleanup. `serve_dashboard()` creates one Runtime, passes its existing handles to the unchanged HTTP server factory, and delegates shutdown to Runtime. Existing cross-venue helper names remain re-exported from `dashboard_web` for compatibility while their implementation moves to the Runtime module.

**Tech Stack:** Python 3.12, `fcntl.flock`, `threading`, `asyncio`, `signal`, pytest, existing Polymarket/Predict clients and SQLite store.

## Global Constraints

- Do not add a `127.0.0.1:8769` listener or change Frontend Gateway routing.
- Do not change API payloads, session/CSRF handling, SQLite schema, strategies, solver/proof logic, modes, or order semantics.
- Keep one-shot `prediction-arb cross-auto` CLI access unchanged in this ticket.
- Runtime construction is side-effect free; only `start()` acquires resources.
- Ownership lock path is `<data_dir>/prediction_arbitrage/runtime.lock`; `execution.lock` remains per-trade.
- Core initialization failures release resources and lock; reconciliation failure holds the lock in `NOT_READY` until explicit stop.
- Duplicate `start()` raises without creating resources; `stop()` is idempotent and best-effort.
- No production handoff occurs until focused tests, contract tests, direct workflow, and final `make acceptance` pass.

---

### Task 1: Add the Runtime lifecycle seam and ownership lock

**Files:**
- Create: `src/open_trader/prediction_runtime.py`
- Create: `tests/test_prediction_runtime.py`

**Interfaces:**
- `PredictionRuntime(*, data_dir: Path, prediction_config_path: Path, dashboard_url: str, notifier: object | None = None, cross_venue_monitor: PredictCrossVenueMonitor | None = None)`
- `PredictionRuntime.start() -> None`
- `PredictionRuntime.stop() -> None`
- `PredictionRuntime.state -> str`
- `PredictionRuntime.store`, `.monitor`, `.cross_venue_monitor`, `.execution` expose the existing handles after construction/start.
- Private `_RuntimeOwnershipLock` acquires/releases `<data_dir>/prediction_arbitrage/runtime.lock` with `fcntl.flock(LOCK_EX | LOCK_NB)`.

- [ ] **Step 1: Write the failing lifecycle tests**

  Add tests that prove the desired interface before production code exists:

  ```python
  def test_runtime_constructor_has_no_side_effects(tmp_path: Path) -> None:
      runtime = PredictionRuntime(
          data_dir=tmp_path,
          prediction_config_path=tmp_path / "prediction.json",
          dashboard_url="http://127.0.0.1:8766/",
      )
      assert runtime.state == "NEW"
      assert not (tmp_path / "prediction_arbitrage" / "runtime.lock").exists()
      assert runtime.store is None
  ```

  Add a real multiprocessing test using `multiprocessing.get_context("spawn")`:
  the first child starts a minimal Runtime and waits on an event, the second
  child attempts the same data directory and reports a lock-conflict result,
  then the first child stops and a third child succeeds. Use a temporary JSON
  config and fake construction collaborators so the test exercises the real
  lock without Keychain/network access.

- [ ] **Step 2: Run the focused tests and verify the expected RED failure**

  Run:

  ```bash
  .venv/bin/python -m pytest -q tests/test_prediction_runtime.py
  ```

  Expected: collection/import failure because `PredictionRuntime` does not yet
  exist. Fix only test syntax or import errors until the failure is specifically
  the missing Runtime implementation.

- [ ] **Step 3: Implement the minimum Runtime state and lock**

  Add the constructor, state guard, owner lock, and cleanup skeleton. The lock
  must create only its parent directory and lock file during `start()`, keep the
  file descriptor open for the Runtime lifetime, and release it exactly once in
  `stop()`. Use explicit states `NEW`, `STARTING`, `RUNNING`, `NOT_READY`,
  `FAILED`, `STOPPING`, and `STOPPED`; make duplicate `start()` and restart of a
  `FAILED` instance raise `RuntimeError` without allocating collaborators.

- [ ] **Step 4: Run the focused tests and verify GREEN**

  Run the same pytest command. Expected: constructor, duplicate-start, stop
  idempotence, and real cross-process lock tests pass.

- [ ] **Step 5: Commit the lifecycle seam**

  ```bash
  git add src/open_trader/prediction_runtime.py tests/test_prediction_runtime.py
  git commit -m "feat: add prediction runtime ownership seam"
  ```

### Task 2: Move Prediction construction and cross-venue lifecycle into Runtime

**Files:**
- Modify: `src/open_trader/prediction_runtime.py`
- Modify: `src/open_trader/dashboard_web.py:30-65,2124-2428`
- Modify: `tests/test_prediction_runtime.py`
- Modify: `tests/test_dashboard_web.py:3010-3290,4580-4610`

**Interfaces:**
- Runtime `start()` constructs the existing `PredictionArbitrageStore`, trading clients, validators, title translator, `PolymarketMonitor`, `PredictionExecutionService`, and optional cross-venue monitor in the existing order.
- Runtime preserves `_UnavailableCrossVenueMonitor`, `_CrossVenueRuntime`, `_cross_venue_gamma_lookup`, and `_build_cross_venue_monitor` behavior; `dashboard_web` imports/re-exports these names so current helper tests remain valid.
- Runtime leaves `create_dashboard_server()` and all `_prediction_*_payload()` functions unchanged.

- [ ] **Step 1: Add failing order and failure tests**

  In `tests/test_prediction_runtime.py`, add fakes that append lifecycle events
  and assert:

  ```text
  owner.acquire, store.open, trading.open, monitor.construct,
  execution.construct, reconcile, polymarket.start, cross.start
  cross.stop, polymarket.stop, execution.close, trading.close,
  store.close, owner.release
  ```

  Add separate tests for core construction failure cleanup, reconciliation
  failure (`NOT_READY`, no monitor start, lock remains held), cross-venue
  construction failure degrading only the cross source, and a cleanup method
  that throws while later cleanup still runs and the owner is not released if a
  thread remains uncertain.

  Update the two existing `serve_dashboard()` lifecycle tests to patch the
  constructors in `open_trader.prediction_runtime`, not `dashboard_web`, so the
  tests prove Legacy invokes one Runtime rather than rebuilding components.

- [ ] **Step 2: Run the new and affected tests and verify RED**

  ```bash
  .venv/bin/python -m pytest -q tests/test_prediction_runtime.py tests/test_dashboard_web.py -k 'prediction_cross_venue_lifecycle or prediction_arbitrage_configured_lifecycle or prediction_runtime'
  ```

  Expected: new Runtime wiring tests fail because the current construction still
  lives in `serve_dashboard()`.

- [ ] **Step 3: Move the existing wiring with no domain changes**

  Move the current `_UnavailableCrossVenueMonitor`, `_CrossVenueRuntime`, gamma
  lookup, and cross monitor builder into `prediction_runtime.py`. Move the
  Prediction branch of `serve_dashboard()` into `PredictionRuntime.start()`;
  preserve the current catch behavior for missing config/Keychain by allowing
  Legacy to render the existing unavailable surface. Keep cross-venue failure
  isolated as the current degraded monitor. Expose Runtime handles to
  `create_dashboard_server()` and call Runtime `stop()` from the existing
  `finally` block.

  Keep Dashboard imports and payload functions compatible by importing the
  moved helper names from `prediction_runtime`. Do not change any API route or
  payload code.

- [ ] **Step 4: Run the affected tests and verify GREEN**

  Run the command from Step 2, then run the contract baseline:

  ```bash
  .venv/bin/python -m pytest -q tests/test_prediction_runtime.py tests/test_dashboard_web.py -k 'prediction_cross_venue_lifecycle or prediction_arbitrage_configured_lifecycle or prediction_runtime'
  .venv/bin/python -m pytest -q tests/test_prediction_api_contract.py
  ```

  Expected: lifecycle tests and all #39 contract tests pass with unchanged
  status/history/mutation semantics.

- [ ] **Step 5: Commit the Runtime extraction**

  ```bash
  git add src/open_trader/prediction_runtime.py src/open_trader/dashboard_web.py tests/test_prediction_runtime.py tests/test_dashboard_web.py
  git commit -m "feat: extract Legacy prediction runtime lifecycle"
  ```

### Task 3: Add graceful signal shutdown and direct Legacy workflow proof

**Files:**
- Modify: `src/open_trader/prediction_runtime.py`
- Modify: `src/open_trader/dashboard_web.py:2277-2430`
- Modify: `tests/test_prediction_runtime.py`
- Modify: `tests/test_dashboard_web.py`

**Interfaces:**
- `serve_dashboard()` temporarily handles `SIGTERM` and `SIGINT` by leaving
  `serve_forever()` and executing its existing Runtime cleanup `finally` path;
  previous handlers are restored before return.
- Forced process termination remains covered by OS lock release and startup
  reconciliation; no signal framework or new service process is introduced.

- [ ] **Step 1: Write the failing signal/workflow tests**

  Add a subprocess test that starts a minimal Legacy dashboard with a fake
  Prediction Runtime, sends `SIGTERM`, waits for exit, and asserts the Runtime
  stop marker was written and the lock can be acquired by a new process. Add a
  `serve_dashboard()` test proving the Runtime is constructed once, server
  starts after Runtime start, and server close follows Runtime stop.

- [ ] **Step 2: Run the tests and verify RED**

  ```bash
  .venv/bin/python -m pytest -q tests/test_prediction_runtime.py tests/test_dashboard_web.py -k 'signal or workflow or lifecycle'
  ```

  Expected: the SIGTERM test fails because current `serve_dashboard()` does not
  route the signal through Runtime cleanup.

- [ ] **Step 3: Implement the minimal signal bridge**

  Install handlers only around `server.serve_forever()`, raise/handle a local
  shutdown signal so the existing `finally` executes, and restore the previous
  handlers in that same `finally`. Do not call `server.shutdown()` from its own
  signal-handler thread; avoid deadlocks.

- [ ] **Step 4: Run focused signal and workflow tests and verify GREEN**

  ```bash
  .venv/bin/python -m pytest -q tests/test_prediction_runtime.py tests/test_dashboard_web.py -k 'signal or workflow or lifecycle'
  ```

- [ ] **Step 5: Commit the graceful shutdown path**

  ```bash
  git add src/open_trader/prediction_runtime.py src/open_trader/dashboard_web.py tests/test_prediction_runtime.py tests/test_dashboard_web.py
  git commit -m "feat: gracefully stop prediction runtime on signals"
  ```

### Task 4: Contract, regression, and operator documentation gate

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `tests/test_prediction_api_contract.py` only if a regression assertion is required; otherwise leave unchanged.

- [ ] **Step 1: Run the full focused Prediction suite**

  ```bash
  .venv/bin/python -m pytest -q tests/test_prediction_runtime.py tests/test_prediction_api_contract.py tests/test_dashboard_web.py tests/test_prediction_arbitrage_execution.py
  ```

  Expected: zero failures; existing #39 contract fields and unavailable/mutation
  semantics remain unchanged.

- [ ] **Step 2: Run direct workflow checks**

  Start the Legacy dashboard in an isolated data directory with no submit-capable
  credentials, call `/healthz` and `/api/prediction-arbitrage/state`, send a
  controlled `SIGTERM`, and verify fresh shutdown/startup logs. Confirm no
  process listens on 8769.

- [ ] **Step 3: Add the dated changelog entry before merge**

  Add a concise operator-facing entry dated 2026-08-10 stating that Legacy now
  owns one lock-protected Prediction Runtime lifecycle, with no 8769 listener,
  Gateway change, or API contract change.

- [ ] **Step 4: Run the final repository checks**

  Run `make test` after restoring the repository's existing ignored Trend
  snapshot fixtures in this worktree, then run the final Dashboard gate:

  ```bash
  make acceptance
  ```

  Only a literal `PASS` permits handoff. If acceptance is `BLOCKED`, report the
  external blocker; if `FAIL`, continue fixing and rerun.

- [ ] **Step 5: Commit the documented result**

  ```bash
  git add CHANGELOG.md
  git commit -m "docs: record prediction runtime extraction"
  ```
