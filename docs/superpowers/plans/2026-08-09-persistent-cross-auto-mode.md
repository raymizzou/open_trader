# Persistent Cross-Venue Auto-Submit Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make SQLite the only authority for cross-venue execution mode so deployments and restarts preserve the operator's automatic-submit state.

**Architecture:** Reuse the existing `cross_auto_state` singleton in `PredictionArbitrageStore`, adding `configured_mode` beside `armed`. The monitor and Dashboard project that state, while the execution claim checks the store transactionally; launchd and environment variables become non-authoritative. A local CLI remains the only mode/arm mutation surface.

**Tech Stack:** Python 3.12, SQLite, existing `PredictionArbitrageStore`, `PredictionExecutionService`, `PredictCrossVenueMonitor`, argparse CLI, Bash launchd installer, plist templates, pytest, and Playwright.

## Global Constraints

- SQLite is the sole source of cross-venue execution authority.
- Allowed configured modes are exactly `observe_only`, `manual_confirm`, and `auto_submit`.
- Normal deployment and restart must not read, infer, or write the configured mode.
- `cross-auto arm` is the only automatic-submit enable action; it must pass existing readiness checks.
- `cross-auto pause` sets `armed=false` while preserving `configured_mode=auto_submit`.
- Explicit non-automatic mode changes disarm automatic submission.
- Missing, malformed, or unreadable state fails closed as `observe_only` and unarmed.
- No retry queue, replay of rejected opportunities, new state service, or second persistence mechanism.
- Existing opportunity, sizing, daily-principal, same-pair, notification-breaker, reconciliation, and residual-position rules remain unchanged.
- Do not run `make acceptance` until the final implementation and runtime proof are ready.

---

## File Map

| File | Responsibility in this plan |
| --- | --- |
| `src/open_trader/prediction_arbitrage_store.py` | Persist/validate `configured_mode`; migrate old rows; atomically gate automatic claims. |
| `src/open_trader/predict_cross_venue.py` | Read durable mode for monitor snapshots and derive effective display mode without an environment override. |
| `src/open_trader/prediction_arbitrage_execution.py` | Read the store as authority, preserve effective-mode truth, and emit stable mode rejection facts. |
| `src/open_trader/cli.py` | Add local `cross-auto mode` and update `arm`/`status` output. |
| `scripts/install_dashboard_launchd.sh` | Reject the retired mode option and stop rendering mode into plists. |
| `ops/launchd/com.open-trader.dashboard.plist.template` | Remove the mode environment variable from the single dashboard job. |
| `ops/launchd/com.open-trader.legacy-dashboard.plist.template` | Remove the mode environment variable from the legacy job. |
| `tests/test_prediction_arbitrage_store.py` | Migration, persistence, corruption, and atomic-claim tests. |
| `tests/test_prediction_arbitrage_execution.py` | Store-authority, pause race, effective mode, and rejection-fact tests. |
| `tests/test_predict_cross_venue.py` | Monitor snapshot mode source and restart/recovery tests. |
| `tests/test_prediction_arbitrage_launchd.py` | CLI mode/arm/status and rejected deployment-argument tests. |
| `tests/test_dashboard_launchd_stack.py` | Installer/plist no-mode regression tests. |
| `tests/test_dashboard_web.py` | Database-projected status and pause-only API tests. |
| `tests/e2e/serve_dashboard_fixture.py` | Paused automatic state and no-manual-action fixture. |
| `tests/e2e/prediction-market.spec.ts` | Browser proof of configured/effective mode and action suppression. |
| `CHANGELOG.md` | Dated operator-facing merge entry before integration to `main`. |

---

### Task 1: Persist configured mode in the existing store

**Files:**
- Modify: `src/open_trader/prediction_arbitrage_store.py` near the `cross_auto_state` schema and state helpers.
- Test: `tests/test_prediction_arbitrage_store.py`.

**Interfaces:**
- Add `PredictionArbitrageStore.set_cross_auto_mode(mode: str, reason: str) -> dict[str, object]`.
- Change `cross_auto_state() -> dict[str, object]` to return `configured_mode`, `armed`, `reason`, and `updated_at`.
- Keep `pause_cross_auto(reason: str) -> dict[str, object]` preserving the current configured mode.
- Make `arm_cross_auto() -> dict[str, object]` persist `configured_mode="auto_submit"` and `armed=true` in one transaction.

- [ ] **Step 1: Write migration and persistence tests first.** Add these tests to `tests/test_prediction_arbitrage_store.py`:

```python
def test_cross_auto_state_defaults_fail_closed_with_configured_mode(tmp_path: Path) -> None:
    assert store(tmp_path).cross_auto_state() == {
        "configured_mode": "observe_only",
        "armed": False,
        "reason": "not_armed",
        "updated_at": None,
    }


def test_cross_auto_mode_and_pause_survive_new_store_instance(tmp_path: Path) -> None:
    db = store(tmp_path)
    assert db.set_cross_auto_mode("auto_submit", "operator_configured")["armed"] is False
    armed = db.arm_cross_auto()
    assert armed["configured_mode"] == "auto_submit"
    assert armed["armed"] is True
    paused = db.pause_cross_auto("operator_paused")
    assert paused["configured_mode"] == "auto_submit"
    assert paused["armed"] is False
    assert store(tmp_path).cross_auto_state() == paused


def test_old_armed_row_migrates_to_observe_only_and_unarmed(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    connection = sqlite3.connect(data_dir / "prediction_arbitrage.sqlite3")
    connection.executescript(
        """
        PRAGMA user_version=4;
        CREATE TABLE cross_auto_state(
            singleton INTEGER PRIMARY KEY CHECK (singleton=1),
            armed INTEGER NOT NULL CHECK (armed IN (0,1)),
            reason TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO cross_auto_state VALUES (1, 1, 'armed', '2026-08-09T00:00:00Z');
        """
    )
    connection.commit()
    connection.close()
    assert PredictionArbitrageStore(data_dir).cross_auto_state()["configured_mode"] == "observe_only"
    assert PredictionArbitrageStore(data_dir).cross_auto_state()["armed"] is False


def test_explicit_nonautomatic_mode_disarms(tmp_path: Path) -> None:
    db = store(tmp_path)
    db.arm_cross_auto()
    state = db.set_cross_auto_mode("manual_confirm", "operator_manual")
    assert state["configured_mode"] == "manual_confirm"
    assert state["armed"] is False
```

- [ ] **Step 2: Run the new store tests and verify they fail.**

Run: `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_prediction_arbitrage_store.py -k 'configured_mode or old_armed_row or explicit_nonautomatic'`

Expected: FAIL because the schema and state payload do not yet contain `configured_mode`.

- [ ] **Step 3: Implement the smallest store migration and state API.**

In the schema migration, add `configured_mode TEXT NOT NULL DEFAULT 'observe_only' CHECK (configured_mode IN ('observe_only', 'manual_confirm', 'auto_submit'))`, then update migrated rows to `armed=0` and reason `migration_fail_closed`. Increment the existing schema version once. Make `_cross_auto_state_from_connection` validate the row and return the fail-closed payload for missing/invalid data. Make `_set_cross_auto_state` preserve the current mode unless a caller explicitly supplies one; make `set_cross_auto_mode` write the requested mode with `armed=0`; make `arm_cross_auto` write both values atomically.

- [ ] **Step 4: Run the store tests and the existing store suite.**

Run: `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_prediction_arbitrage_store.py`

Expected: PASS, including the existing 80+ store tests and the new migration/state tests.

- [ ] **Step 5: Commit the store change.**

```bash
git add src/open_trader/prediction_arbitrage_store.py tests/test_prediction_arbitrage_store.py
git commit -m "feat: persist cross auto configured mode"
```

### Task 2: Make monitor and execution use the durable authority

**Files:**
- Modify: `src/open_trader/predict_cross_venue.py` in `PredictCrossVenueMonitor` construction and `snapshot()`.
- Modify: `src/open_trader/dashboard_web.py` in `_build_cross_venue_monitor`.
- Modify: `src/open_trader/prediction_arbitrage_execution.py` in `_configured_cross_execution_mode`, `cross_auto_status`, `auto_submit_cross_venue`, and the mode-reason map.
- Test: `tests/test_predict_cross_venue.py`.
- Test: `tests/test_prediction_arbitrage_execution.py`.

**Interfaces:**
- `PredictCrossVenueMonitor` receives the existing `store` and exposes the durable `configured_mode` in its root snapshot; no constructor environment mode is used.
- `PredictionExecutionService._configured_cross_execution_mode() -> str` reads `self._store.cross_auto_state()["configured_mode"]` and fail-closes on exceptions.
- `PredictionArbitrageStore.claim_cross_auto_attempt(...)` returns a claim result that distinguishes `claimed`, `signal_already_attempted`, and a durable-mode rejection while keeping the one-shot row and all existing limits atomic.

- [ ] **Step 1: Write authority and concurrency tests first.** Add tests covering:

```python
def test_execution_mode_comes_from_store_when_monitor_snapshot_disagrees(tmp_path: Path) -> None:
    service, store, _trading, cross, _predict = _cross_service(tmp_path)
    store.set_cross_auto_mode("auto_submit", "operator_configured")
    cross.overrides["execution_mode"] = "observe_only"
    assert service._configured_cross_execution_mode() == "auto_submit"


def test_pause_before_claim_records_cross_auto_paused_without_order(tmp_path: Path) -> None:
    service, store, trading, cross, predict = _cross_service(tmp_path)
    store.set_cross_auto_mode("auto_submit", "operator_configured")
    store.pause_cross_auto("operator_paused")
    signal_id = _cross_venue_notification_signal(store)
    result = service.notify_ready_opportunity(
        "cross:public-pair:PREDICT_YES_POLYMARKET_NO", signal_id
    )
    assert result["reason"] == "cross_auto_paused"
    assert result["facts"]["current"] == "paused"
    assert store.cross_auto_attempts()[0]["reason_code"] == "cross_auto_paused"
    assert (predict.submit_calls, trading.cross_submit_calls) == (0, 0)


def test_pause_after_submission_started_does_not_interrupt_reconciliation(tmp_path: Path) -> None:
    service, store, _trading, cross, predict = _cross_service(tmp_path)
    cross.overrides["execution_mode"] = "auto_submit"
    store.arm_cross_auto()
    signal_id = _cross_venue_notification_signal(store)
    accepted = service.auto_submit_cross_venue(
        "cross:public-pair:PREDICT_YES_POLYMARKET_NO", signal_id
    )
    assert accepted.get("execution_id")
    store.pause_cross_auto("operator_paused")
    final = wait_until_terminal(service, str(accepted["execution_id"]))
    assert final["state"] == "holding_to_resolution"
    assert predict.submit_calls == 1
```

Also update the existing degraded/empty-book status test to assert that the configured mode remains `auto_submit` while `effective_mode` reflects the existing readiness rule, rather than trusting a monitor environment override.

- [ ] **Step 2: Run the focused tests and verify they fail.**

Run: `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_predict_cross_venue.py tests/test_prediction_arbitrage_execution.py -k 'store or pause or configured_mode or cross_auto_status'`

Expected: FAIL because the service currently reads `snapshot()["mode"]` and claims an attempt before the durable mode gate.

- [ ] **Step 3: Implement store-authority reads and one transaction gate.**

Change the dashboard monitor builder to pass the existing store without `os.environ`. Keep the monitor root mode compatible for current projections, but derive it from `store.cross_auto_state()` and expose `configured_mode` explicitly. In the execution service, read the store directly for configured mode. Move the durable `configured_mode=auto_submit` and `armed=true` check into the same SQLite transaction that inserts the one-shot claim; preserve existing duplicate-signal, same-pair, daily-principal, unsettled-principal, and no-queue behavior. Return the stable mode reason to the existing `_finish_cross_auto_rejection` path so it writes the normal operator facts. Do not interrupt an execution after `confirm` has begun.

- [ ] **Step 4: Run the focused suites and then all cross-venue tests.**

Run: `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_predict_cross_venue.py tests/test_prediction_arbitrage_execution.py`

Expected: PASS with the existing cross-venue rules unchanged and the new mode/concurrency assertions green.

- [ ] **Step 5: Commit the authority change.**

```bash
git add src/open_trader/predict_cross_venue.py src/open_trader/dashboard_web.py src/open_trader/prediction_arbitrage_execution.py tests/test_predict_cross_venue.py tests/test_prediction_arbitrage_execution.py
git commit -m "feat: read cross auto mode from durable state"
```

### Task 3: Add local mode commands and remove launchd mode mutation

**Files:**
- Modify: `src/open_trader/cli.py` parser and `prediction-arb cross-auto` dispatch.
- Modify: `scripts/install_dashboard_launchd.sh` option parsing and template rendering.
- Modify: `ops/launchd/com.open-trader.dashboard.plist.template`.
- Modify: `ops/launchd/com.open-trader.legacy-dashboard.plist.template`.
- Test: `tests/test_prediction_arbitrage_launchd.py`.
- Test: `tests/test_dashboard_launchd_stack.py`.

**Interfaces:**
- Register `prediction-arb cross-auto mode {observe_only,manual_confirm,auto_submit} --data-dir PATH`.
- Keep `prediction-arb cross-auto arm --data-dir PATH --url LOOPBACK --expected-sha SHA` as the readiness-checked enable action.
- Keep `prediction-arb cross-auto pause --data-dir PATH` local-only if an existing caller needs it; it must preserve configured mode.
- `scripts/install_dashboard_launchd.sh --cross-execution-mode ...` exits 2 before `launchctl`, writes no plist, and prints the local CLI guidance.

- [ ] **Step 1: Write CLI and installer regression tests first.** Add assertions that:

```python
def test_cross_auto_mode_command_changes_only_local_state(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([
        "prediction-arb", "cross-auto", "mode", "auto_submit",
        "--data-dir", str(tmp_path),
    ]) == 0
    state = PredictionArbitrageStore(tmp_path).cross_auto_state()
    assert state["configured_mode"] == "auto_submit"
    assert state["armed"] is False
    assert "configured_mode: auto_submit" in capsys.readouterr().out


def test_retired_installer_mode_option_fails_without_launchctl_or_database_write(tmp_path: Path) -> None:
    result = subprocess.run([str(INSTALLER), "--dry-run", "--mode", "single",
                             "--repo-root", str(ROOT), "--runtime-root", str(tmp_path),
                             "--cross-execution-mode", "auto_submit"],
                            cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 2
    assert "cross-auto mode" in result.stderr


def test_dashboard_plists_have_no_cross_execution_environment_variable() -> None:
    for template in (SINGLE_TEMPLATE, LEGACY_TEMPLATE):
        assert "OPEN_TRADER_CROSS_EXECUTION_MODE" not in template.read_text(encoding="utf-8")
```

- [ ] **Step 2: Run the new CLI/installer tests and verify they fail.**

Run: `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_prediction_arbitrage_launchd.py tests/test_dashboard_launchd_stack.py -k 'cross_auto_mode or retired_installer or cross_execution or plist'`

Expected: FAIL because the parser has no `mode` command and the installer currently renders the environment variable.

- [ ] **Step 3: Implement local-only commands and fail-visible installer behavior.**

Print configured mode, effective mode, armed state, pause reason, daily principal, and latest attempt in `status`. Add `mode` dispatch using the store method; reject invalid modes through argparse choices. Keep `arm` readiness checks, but require the remote state to report `configured_mode=auto_submit` before writing `arm_cross_auto()`. Make the installer reject any occurrence of `--cross-execution-mode` before path/bootstrap side effects, remove its default and substitution, and remove the corresponding environment entries from both plist templates.

- [ ] **Step 4: Run the full CLI and launchd focused suites.**

Run: `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_prediction_arbitrage_launchd.py tests/test_dashboard_launchd_stack.py`

Expected: PASS; no dry-run or rejected invocation calls `launchctl`, and no rendered plist contains the mode variable.

- [ ] **Step 5: Commit the CLI/launchd change.**

```bash
git add src/open_trader/cli.py scripts/install_dashboard_launchd.sh ops/launchd/com.open-trader.dashboard.plist.template ops/launchd/com.open-trader.legacy-dashboard.plist.template tests/test_prediction_arbitrage_launchd.py tests/test_dashboard_launchd_stack.py
git commit -m "feat: make cross auto mode local-only"
```

### Task 4: Make Dashboard state truthful and suppress manual actions while paused

**Files:**
- Modify: `src/open_trader/prediction_arbitrage_execution.py` only if status projection needs the shared effective-mode helper.
- Modify: `src/open_trader/dashboard_web.py` and `src/open_trader/dashboard_static/dashboard.js`.
- Modify: `tests/e2e/serve_dashboard_fixture.py`.
- Test: `tests/test_dashboard_web.py`.
- Test: `tests/e2e/prediction-market.spec.ts`.

**Interfaces:**
- `/api/prediction-arbitrage/state` exposes database-backed `configured_mode`, derived `effective_mode`, `armed`, `pause_reason`, and latest stable rejection facts.
- The browser renders no `[data-action=participate]` for a `manual_only` candidate when `cross_auto.configured_mode === "auto_submit"`, including paused/degraded state.
- The only cross-auto mutation route remains confirmed, CSRF-protected `POST /api/prediction-arbitrage/cross-auto/pause`.

- [ ] **Step 1: Write the paused-state API and browser assertions first.** Add a fixture scenario with `configured_mode=auto_submit`, `effective_mode=observe_only`, `armed=false`, and a manual-only candidate. Assert the state response includes the durable configured mode and the browser shows the Chinese pause explanation with zero participate buttons. Assert `/cross-auto/arm` remains 404 and `/cross-auto/pause` rejects missing CSRF or confirmation.

- [ ] **Step 2: Run the new Dashboard tests and verify the regression fails if present.**

Run: `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_dashboard_web.py -k 'cross_auto' && npm exec playwright test tests/e2e/prediction-market.spec.ts -g 'paused.*manual|cross-auto' --project=chromium`

Expected: the paused/manual-only test fails if candidate suppression uses effective mode; existing safe-pause tests remain green.

- [ ] **Step 3: Implement the minimal projection/UI fix.**

Build the `cross_auto` payload from `PredictionExecutionService.cross_auto_status()` and the store state; never substitute an environment value. Keep configured and effective mode separate. In `dashboard.js`, use `configured_mode === "auto_submit"` to suppress manual-only participation while paused; render the stable Chinese reason and required action. Do not add arm or mode-change controls.

- [ ] **Step 4: Run Dashboard focused tests and the complete prediction-market browser suite.**

Run: `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_dashboard_web.py -k 'cross_auto' && npm exec playwright test tests/e2e/prediction-market.spec.ts --project=chromium`

Expected: focused API tests pass and all prediction-market browser cases pass with no manual action shown for paused automatic configuration.

- [ ] **Step 5: Commit the Dashboard change.**

```bash
git add src/open_trader/dashboard_web.py src/open_trader/dashboard_static/dashboard.js tests/test_dashboard_web.py tests/e2e/serve_dashboard_fixture.py tests/e2e/prediction-market.spec.ts
git commit -m "fix: keep paused cross auto state truthful"
```

### Task 5: Integrate, accept, and prove deployment preserves operator state

**Files:**
- Modify: `CHANGELOG.md` with the dated operator-facing entry before merge.
- Runtime: local `data/prediction_arbitrage.sqlite3`, launchd jobs, logs, and dashboard URL; do not commit runtime state.

- [ ] **Step 1: Run the complete automated suites before the final gate.**

Run: `PYTHONPATH=src .venv/bin/python -m pytest -q`

Expected: all tests pass with only the repository's existing warnings.

- [ ] **Step 2: Run direct non-mutating installer and state checks.**

Pre-seed a disposable SQLite row with `configured_mode=auto_submit, armed=true`; run the installer dry-run with no mode option and assert the row is unchanged. Run it with the retired mode option and assert exit 2, no plist write, and no `launchctl` call. Verify rendered plists contain no mode environment variable.

- [ ] **Step 3: Update and commit `CHANGELOG.md` before merging.**

Record the persistent-state authority, local-only arm/mode commands, fail-closed migration, deployment rejection behavior, and verification scope in a dated `2026-08-09` operator entry. Commit the changelog on the feature branch before any merge to `main`.

- [ ] **Step 4: Run the final Dashboard gate.**

Run: `make acceptance`

Expected: `PASS`. Do not call the change accepted or deployed on `FAIL` or `BLOCKED`.

- [ ] **Step 5: Merge and redeploy the exact accepted SHA.**

Fast-forward the accepted branch to local `main`, push the exact SHA, and redeploy the same SHA. The deployment command must omit `--cross-execution-mode`. Do not run `cross-auto arm` automatically as part of deployment.

- [ ] **Step 6: Verify live state and preserve it.**

Inspect `launchctl` PID, working directory, process SHA, fresh logs, and HTTP 200. Run local `prediction-arb cross-auto status` and record `configured_mode`, `effective_mode`, `armed`, and the latest attempt count. Confirm the SQLite row is unchanged across the restart and that no new execution/notification was created by deployment. Only then hand the review URL to the user.

---

## Self-review checklist

- Store migration and corrupt-state fallback are covered by Task 1.
- Store-vs-monitor authority and pause-before-claim race are covered by Task 2.
- Local-only mode changes and retired installer argument are covered by Task 3.
- Dashboard truthfulness, secret redaction, pause-only route, and manual-action suppression are covered by Task 4.
- Changelog-before-merge, final acceptance, exact-SHA redeploy, and runtime proof are covered by Task 5.
- No task introduces a new persistence mechanism, retry queue, web arm action, or unrelated strategy change.
