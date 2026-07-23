# Drawdown Bootstrap and Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Safely initialize and recover v4 drawdown baselines for CN, HK, and US without allowing report generation to reset risk state.

**Architecture:** Keep state transitions and hashed snapshots in `strategy_drawdown.py`; add one orchestration module for report evidence, market-independent preflight, alert deduplication, and structured results. Expose it through a CLI command used by the final acceptance workflow, while existing report commands remain observation-only.

**Tech Stack:** Python 3.12, stdlib JSON/SHA-256/file locking, pytest, existing Futu clients, existing notifier and vanilla Dashboard JavaScript.

## Global Constraints

- Start from local `main` in an isolated branch/worktree.
- Do not add dependencies or a new Dashboard page.
- Automatic bootstrap is legal only for first activation or a new strategy version.
- Same-version redeploys never reset high-water marks, drawdown, pause state, or audit history.
- Report generation never initializes or restores drawdown state.
- State loss/corruption restores only from a hash-valid immutable snapshot; otherwise fail closed.
- CN, HK, and US preflight independently; one unavailable market must not mutate another market's result.
- Existing sell/hold/protection watcher behavior must remain independent of entry eligibility.
- Do not implement external cash-flow adjustment.
- Do not run `make acceptance` until the final gate.

## File Map

- `src/open_trader/strategy_drawdown.py`: canonical parameter hashing, automatic bootstrap transition, richer decisions, snapshot persistence and recovery.
- `src/open_trader/drawdown_preflight.py`: frozen-report evidence scan, market preflight orchestration, entry-window cutoff, alert ledger, structured outcomes.
- `src/open_trader/cli.py`: `trend-drawdown-preflight` parser and real CN/HK/US adapters.
- `src/open_trader/dashboard.py`: project bootstrap/audit fields already frozen in `drawdown_summary`.
- `src/open_trader/dashboard_static/dashboard.js`: render baseline and audit details in the existing drawdown section.
- `src/open_trader/dashboard_acceptance.py`: require healthy v4 drawdown projections and browser-visible baseline/blocker details.
- `Makefile`: run real drawdown preflight between the full test suite and Dashboard acceptance.
- `tests/test_strategy_drawdown.py`: state identity, idempotency, snapshots, recovery, and sticky-pause regression coverage.
- `tests/test_drawdown_preflight.py`: evidence classification, per-market outcomes, cutoff, and alert deduplication.
- `tests/test_strategy_drawdown_cli.py`: parser and real-adapter wiring.
- `tests/test_dashboard.py`, `tests/test_dashboard_web.py`, `tests/test_dashboard_acceptance.py`: projection and acceptance coverage.

---

### Task 1: Automatic Bootstrap Identity

**Files:**
- Modify: `src/open_trader/strategy_drawdown.py`
- Test: `tests/test_strategy_drawdown.py`

**Interfaces:**
- Produces: `strategy_parameter_hash(parameters: Mapping[str, object]) -> str`.
- Produces: `automatic_bootstrap_strategy_drawdown(data_dir: Path, *, market: str, strategy_id: str, strategy_version: str, parameters: Mapping[str, object], baseline_equity: Decimal, source_date: str, accepted_git_sha: str, actor: str, occurred_at: str, reason: str, entry_eligible_from: str) -> dict[str, object]`.
- Existing `observe_strategy_equity(...)` and `manual_unlock_strategy_drawdown(...)` signatures remain unchanged.

- [ ] **Step 1: Write failing identity and bootstrap tests**

Add tests that assert canonical dict key ordering yields the same 64-character hash, first bootstrap records an `automatic_bootstrap` event with all required fields, replay is byte-idempotent, a new Git SHA with identical parameters is accepted, and a parameter change under the same key raises `ValueError("strategy parameters changed without a version bump")`.

```python
def test_automatic_bootstrap_is_idempotent_by_strategy_parameters(tmp_path: Path) -> None:
    request = dict(
        market="CN", strategy_id="trend_animals_warm_to_hot/CN/v4",
        strategy_version="v4", parameters={"position_limit": 10},
        baseline_equity=Decimal("100000"), source_date="2026-07-17",
        accepted_git_sha="a" * 40, actor="deployment",
        occurred_at="2026-07-20T08:00:00+08:00",
        reason="first_activation", entry_eligible_from="2026-07-20",
    )
    automatic_bootstrap_strategy_drawdown(tmp_path / "data", **request)
    before = (tmp_path / "data/trend_drawdown/state.json").read_bytes()
    request["accepted_git_sha"] = "b" * 40
    automatic_bootstrap_strategy_drawdown(tmp_path / "data", **request)
    assert (tmp_path / "data/trend_drawdown/state.json").read_bytes() == before
```

- [ ] **Step 2: Run RED tests**

Run: `PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_strategy_drawdown.py -k 'parameter_hash or automatic_bootstrap'`

Expected: collection/import failure because the new functions do not exist.

- [ ] **Step 3: Implement the minimal transition**

Use canonical JSON and stdlib SHA-256:

```python
def strategy_parameter_hash(parameters: Mapping[str, object]) -> str:
    encoded = json.dumps(
        dict(parameters), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
```

Extend `_valid_audit_event` to validate the existing `manual_unlock` shape or the exact `automatic_bootstrap` shape. Find the automatic event for an existing key to compare hashes. Create the baseline with `_new_record`; never modify an existing matching record or event.

- [ ] **Step 4: Extend decisions with bootstrap audit context**

Add one nullable `bootstrap_event` field to `DECISION_FIELDS`. Missing/corrupt decisions return `None`; valid records return the matching automatic event. Update `valid_drawdown_decision` to validate this field without requiring one for legacy/manual state.

- [ ] **Step 5: Run GREEN and regression tests**

Run: `PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_strategy_drawdown.py tests/test_trend_review.py tests/test_a_share_trend.py tests/test_market_trend.py`

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/open_trader/strategy_drawdown.py tests/test_strategy_drawdown.py tests/test_trend_review.py tests/test_a_share_trend.py tests/test_market_trend.py
git commit -m "feat: add audited drawdown bootstrap"
```

### Task 2: Immutable Snapshots and Recovery

**Files:**
- Modify: `src/open_trader/strategy_drawdown.py`
- Test: `tests/test_strategy_drawdown.py`

**Interfaces:**
- Produces: `recover_strategy_drawdown_state(data_dir: Path) -> dict[str, object]` returning `{"status": "recovered", "snapshot": str, "state_sha256": str}`.
- `_write_state(path, payload)` additionally creates a unique immutable snapshot for every distinct valid state payload.

- [ ] **Step 1: Write failing snapshot tests**

Cover snapshot creation after bootstrap/observe/unlock, no overwrite on identical payload, restoration of the exact sticky paused state after deleting `state.json`, newest-invalid fallback to an older valid snapshot, and failure without modifying a corrupt live state when no snapshot validates.

```python
def test_recovery_restores_sticky_pause_from_latest_valid_snapshot(tmp_path: Path) -> None:
    # bootstrap at 100, observe 94 to pause, then delete live state
    state_path.unlink()
    result = recover_strategy_drawdown_state(data_dir)
    restored = json.loads(state_path.read_text())
    assert result["status"] == "recovered"
    assert restored["records"][0]["paused"] is True
    assert restored["records"][0]["high_water_mark"] == "100"
```

- [ ] **Step 2: Run RED tests**

Run: `PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_strategy_drawdown.py -k 'snapshot or recover'`

Expected: failure because snapshots and recovery are absent.

- [ ] **Step 3: Implement snapshot envelopes**

Canonicalize state bytes using the same sorted/indented JSON format as `state.json`. Store exact envelopes under `data/trend_drawdown/snapshots/<sha256>.json`:

```json
{
  "schema_version": "open_trader.strategy_drawdown_snapshot.v1",
  "state": {},
  "state_sha256": "64 lowercase hex characters"
}
```

Write with `NamedTemporaryFile` plus `Path.replace`; if the digest path already exists, validate it and leave it unchanged. Recovery scans files by modification time newest-first, validates digest and state, and atomically restores the first valid payload.

- [ ] **Step 4: Preserve fail-closed observation**

Do not call recovery from `observe_strategy_equity`. Missing/corrupt observations must keep returning blocked decisions. Recovery is callable only from preflight.

- [ ] **Step 5: Run GREEN and atomic-write regressions**

Run: `PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_strategy_drawdown.py`

Expected: all tests pass, including injected state replace failure.

- [ ] **Step 6: Commit**

```bash
git add src/open_trader/strategy_drawdown.py tests/test_strategy_drawdown.py
git commit -m "feat: recover drawdown state from hashed snapshots"
```

### Task 3: Three-Market Preflight and CLI

**Files:**
- Create: `src/open_trader/drawdown_preflight.py`
- Modify: `src/open_trader/cli.py`
- Test: `tests/test_drawdown_preflight.py`
- Test: `tests/test_strategy_drawdown_cli.py`

**Interfaces:**
- Produces: `run_drawdown_preflight(*, data_dir: Path, reports_dir: Path, market_inputs: Mapping[str, DrawdownMarketInput], accepted_git_sha: str, actor: str, occurred_at: str, notifier: Notifier) -> dict[str, object]`.
- `DrawdownMarketInput` contains market, strategy snapshot, baseline equity, source date, and entry eligibility date.
- CLI: `open-trader trend-drawdown-preflight --config PATH --repo PATH --actor TEXT`.

- [ ] **Step 1: Write failing evidence and orchestration tests**

Use frozen report JSON fixtures across `trend_a_share`, `trend_hk_phillips`, and `trend_us_tiger`. Assert:

- absent state plus no historical `ok` permits first activation;
- absent/corrupt state plus any historical `ok` attempts snapshot recovery and otherwise fails;
- a valid state may add a new version independently;
- one unavailable market returns `unavailable` while others bootstrap;
- late initialization sets `entry_eligible_from` to the next trading date;
- existing report bytes remain unchanged.

- [ ] **Step 2: Run RED tests**

Run: `PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_drawdown_preflight.py`

Expected: import failure because `drawdown_preflight.py` does not exist.

- [ ] **Step 3: Implement evidence scan and market loop**

Use exact report directories and accept historical proof only when a JSON object has a matching market plus `drawdown_summary.state_status == "ok"`. Return ordered CN/HK/US results and an overall status; catch availability errors per market, but classify integrity errors as `failed`.

- [ ] **Step 4: Write failing CLI adapter tests**

Patch Futu quote/account factories and assert the command:

- computes `accepted_git_sha` from `--repo` HEAD;
- obtains distinct configured account IDs;
- resolves the latest completed market trading date;
- uses `live_trend_strategy_snapshot` for all markets;
- prints structured JSON and exits `0` only when all markets are ready.

- [ ] **Step 5: Implement CLI wiring**

Add parser options with safe defaults:

```python
preflight = subparsers.add_parser("trend-drawdown-preflight")
preflight.add_argument("--config", type=Path, default=Path("config/daily_premarket.env"))
preflight.add_argument("--repo", type=Path, default=Path.cwd())
preflight.add_argument("--actor", default="deployment")
```

Resolve the last completed trading date from `FutuQuoteClient.get_trading_days`; close the quote client in `finally`. Prefer a matching frozen missing-state report's account equity/source date, otherwise freeze the Futu simulation account snapshot for that completed date.

- [ ] **Step 6: Run GREEN tests**

Run: `PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_drawdown_preflight.py tests/test_strategy_drawdown_cli.py tests/test_premarket_cli.py`

Expected: all selected tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/open_trader/drawdown_preflight.py src/open_trader/cli.py tests/test_drawdown_preflight.py tests/test_strategy_drawdown_cli.py tests/test_premarket_cli.py
git commit -m "feat: add three-market drawdown preflight"
```

### Task 4: Alert Deduplication and Dashboard Projection

**Files:**
- Modify: `src/open_trader/drawdown_preflight.py`
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `src/open_trader/dashboard.py`
- Test: `tests/test_drawdown_preflight.py`
- Test: `tests/test_dashboard.py`
- Test: `tests/test_dashboard_web.py`

**Interfaces:**
- Alert ledger: `data/trend_drawdown/alerts.json`, keyed by `market|strategy_version|failure_status`.
- Dashboard continues to consume the frozen report's `drawdown_summary`; no new endpoint or card.

- [ ] **Step 1: Write failing alert tests**

Assert the first missing/corrupt/recovery-failed outcome calls `notifier.notify`, repetition does not call it again, a healthy outcome clears the active key, and recurrence alerts again. Assert notification exceptions do not change the preflight result.

- [ ] **Step 2: Run RED alert tests**

Run: `PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_drawdown_preflight.py -k alert`

Expected: failures because no ledger exists.

- [ ] **Step 3: Implement the minimal alert ledger**

Atomically persist exact active keys and send one high-priority title/message through the supplied notifier. Clear only a recovered market/version's prior active keys.

- [ ] **Step 4: Write failing Dashboard tests**

Add a v4 report whose `drawdown_summary.bootstrap_event` contains baseline equity, source date, SHA, hash, actor, and event ID. Assert the projection preserves it and `renderTrendRiskSummary` includes “基准已自动建立”, baseline/source date, and escaped audit values inside the existing section.

- [ ] **Step 5: Run RED Dashboard tests**

Run: `PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_dashboard.py tests/test_dashboard_web.py -k 'drawdown or bootstrap'`

Expected: browser-render assertion fails before JavaScript changes.

- [ ] **Step 6: Implement Dashboard rendering**

Keep `dashboard.py` pass-through behavior. Add a compact bootstrap paragraph and `<details>` audit block to `.trend-drawdown-summary`; apply `escapeHtml(formatPlain(...))` to every value.

- [ ] **Step 7: Run GREEN tests**

Run: `PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_drawdown_preflight.py tests/test_dashboard.py tests/test_dashboard_web.py`

Expected: all selected tests pass.

- [ ] **Step 8: Commit**

```bash
git add src/open_trader/drawdown_preflight.py src/open_trader/dashboard.py src/open_trader/dashboard_static/dashboard.js tests/test_drawdown_preflight.py tests/test_dashboard.py tests/test_dashboard_web.py
git commit -m "feat: surface drawdown bootstrap health"
```

### Task 5: Acceptance and Deployment Preflight

**Files:**
- Modify: `src/open_trader/dashboard_acceptance.py`
- Modify: `tests/test_dashboard_acceptance.py`
- Modify: `Makefile`

**Interfaces:**
- `make acceptance` runs the full tests, then real `trend-drawdown-preflight`, then `dashboard_acceptance`.
- The preflight command's nonzero outcome stops acceptance before browser review.

- [ ] **Step 1: Write failing acceptance tests**

Assert v4 integrated candidates require `drawdown_summary.state_status == "ok"`, no missing-state risk skip, and the real browser sees the existing drawdown area plus bootstrap/blocking text when present.

- [ ] **Step 2: Run RED acceptance tests**

Run: `PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_dashboard_acceptance.py -k drawdown`

Expected: new health/browser assertions fail.

- [ ] **Step 3: Implement acceptance checks and Makefile ordering**

Insert after the full pytest command and before `dashboard_acceptance`:

```make
	cd "$(WORKTREE_ROOT)" && \
		PYTHONPATH=src .venv/bin/python -m open_trader trend-drawdown-preflight \
		--config "$(REPOSITORY_ROOT)/config/daily_premarket.env" \
		--repo "$(WORKTREE_ROOT)" --actor acceptance
```

Use the worktree Python path already selected by the Makefile if `.venv` is shared; do not change dependency locks.

- [ ] **Step 4: Run focused GREEN tests**

Run: `PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q tests/test_dashboard_acceptance.py tests/test_strategy_drawdown.py tests/test_drawdown_preflight.py tests/test_strategy_drawdown_cli.py`

Expected: all selected tests pass.

- [ ] **Step 5: Run the full automated suite**

Run: `PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q`

Expected: zero failures.

- [ ] **Step 6: Commit**

```bash
git add src/open_trader/dashboard_acceptance.py tests/test_dashboard_acceptance.py Makefile
git commit -m "test: gate deployment on drawdown preflight"
```

### Task 6: Review, Live Workflow, and Final Gate

**Files:**
- Review all changes since `dd58a0c`.
- No source edits after the final passing acceptance gate.

**Interfaces:**
- Consumes the complete implementation.
- Produces the exact accepted Git SHA and live review URL evidence.

- [ ] **Step 1: Run code review**

Use the repository `code-review` skill against baseline `dd58a0c`. Fix every confirmed Standards or Spec issue, rerun affected tests, and commit fixes before continuing.

- [ ] **Step 2: Run the real preflight directly**

Run from the issue worktree with the shared production config:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m open_trader \
  trend-drawdown-preflight \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env \
  --repo "$PWD" --actor issue-11-deployment
```

Expected: CN, HK, and US each report `ready`, `bootstrapped`, or `recovered`; otherwise follow Issue #11's `FAIL`/`BLOCKED` rules.

- [ ] **Step 3: Run real report workflows only when the time-window rule permits**

Run the three report commands with `--revision` only if preflight says the current execution date remains eligible. Otherwise preserve the current frozen files and schedule verification for the next trading day. Record exact JSON output and report SHA-256 values.

- [ ] **Step 4: Inspect long-running processes before the gate**

Run:

```bash
launchctl list | rg 'com.open-trader.trend|open-trader'
screen -ls
ps -axo pid,lstart,command | rg 'open_trader|dashboard' | rg -v 'rg '
```

Stop or restart only stale in-scope Dashboard/report processes using the repository's existing launchd/screen procedure. Verify PID, cwd, Git SHA, and fresh logs.

- [ ] **Step 5: Commit any remaining source changes, then run the final gate once**

Run: `make acceptance`

Expected: literal `PASS`. On `FAIL`, diagnose/fix/commit and rerun; on `BLOCKED`, report the blocker and do not substitute fixtures or curl.

- [ ] **Step 6: Redeploy the exact accepted SHA**

Restart the Dashboard from the accepted worktree/SHA. Verify:

```bash
lsof -nP -iTCP:8766 -sTCP:LISTEN
ps -p "$PID" -o pid=,lstart=,command=
lsof -a -p "$PID" -d cwd -Fn
git -C "$RUNNING_CWD" rev-parse HEAD
tail -n 50 /tmp/open_trader_dashboard_8766.log
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/
```

Expected: new PID, accepted cwd/SHA, fresh log timestamp, and HTTP `200`.
