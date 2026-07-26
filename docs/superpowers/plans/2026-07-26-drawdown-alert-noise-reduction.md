# Drawdown Alert Noise Reduction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop deployment acceptance attempts from sending external drawdown alerts and consolidate real multi-market drawdown failures into one actionable Chinese notification.

**Architecture:** Keep the existing `actor`, notifier protocol, preflight result model, and JSON alert ledger. Select `NullNotifier` at the CLI boundary when `actor=acceptance`; for all other actors, retain the configured notifier and batch newly active market/version/failure keys inside `_sync_failure_alerts`.

**Tech Stack:** Python 3.12, stdlib JSON/pathlib/tempfile, existing notifier classes, pytest.

## Global Constraints

- Work only in `/Users/ray/projects/open_trader/.worktrees/reduce-drawdown-alert-noise` on branch `fix/reduce-drawdown-alert-noise`, based on local `main` at `ffe0bec`.
- Add no dependency, environment variable, CLI flag, retry queue, timer, central notification service, or notifier abstraction.
- Preserve all drawdown calculations, state inheritance, fail-closed entry blocking, sell/protection behavior, command exit codes, and Dashboard data.
- `actor=acceptance` must suppress all external notification channels while retaining real preflight reads, legal state writes, logs, and exit status.
- Non-acceptance actors must retain configured notification delivery.
- One preflight run may send at most one drawdown failure notification, while the ledger continues to track each `market|strategy_version|failure_status` key independently.
- User-facing notification text must be Chinese and must not include internal English errors, stack traces, or local paths.
- Do not run `make acceptance`; this task changes no Dashboard behavior. Run the real `trend-drawdown-preflight` command with `actor=acceptance` as the direct workflow check.
- Follow red-green-refactor: observe each behavior test fail for the intended reason before changing production code.

---

## File Map

- Modify `src/open_trader/cli.py`: choose `NullNotifier` for acceptance and the configured notifier for every other actor.
- Modify `tests/test_strategy_drawdown_cli.py`: prove acceptance does not construct a real notifier and default deployment still does.
- Modify `src/open_trader/drawdown_preflight.py`: translate stable failure categories, batch new alert keys, send one message, and persist keys only after delivery succeeds.
- Modify `tests/test_drawdown_preflight.py`: cover exact grouped copy, deduplication, recovery rearming, generic fallback text, and notification failure.
- Modify `CHANGELOG.md`: add the dated operator-facing entry after verification.

---

### Task 1: Silence Acceptance Notifications at the CLI Boundary

**Files:**
- Modify: `tests/test_strategy_drawdown_cli.py:156-244`
- Modify: `tests/test_strategy_drawdown_cli.py:403-472`
- Modify: `src/open_trader/cli.py:1431-1439`

**Interfaces:**
- Consumes: `args.actor: str`, existing `NullNotifier`, and `build_notifier(config)`.
- Produces: the existing `run_drawdown_preflight(..., notifier: Notifier)` call with a notifier selected by actor.

- [ ] **Step 1: Add acceptance and deployment routing assertions**

In `test_trend_drawdown_preflight_cli_bootstraps_all_markets_independently`, replace the current `build_notifier` monkeypatch with a function that fails if acceptance tries to build a configured notifier:

```python
def unexpected_build_notifier(config: object) -> object:
    raise AssertionError("acceptance must not build an external notifier")

monkeypatch.setattr(cli, "build_notifier", unexpected_build_notifier)
```

Keep the existing CLI arguments containing:

```python
"--actor", "acceptance",
```

In `test_trend_drawdown_preflight_reuses_existing_audited_state_without_new_baseline`, replace its notifier monkeypatch with a recording builder:

```python
built_notifiers: list[object] = []

def build_recording_notifier(current_config: object) -> cli.NullNotifier:
    built_notifiers.append(current_config)
    return cli.NullNotifier()

monkeypatch.setattr(cli, "build_notifier", build_recording_notifier)
```

After the existing result assertions, add:

```python
assert built_notifiers == [config]
```

This second CLI invocation omits `--actor`, so it exercises the existing default `deployment` actor.

- [ ] **Step 2: Run the routing tests and verify the acceptance case fails**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_strategy_drawdown_cli.py::test_trend_drawdown_preflight_cli_bootstraps_all_markets_independently \
  tests/test_strategy_drawdown_cli.py::test_trend_drawdown_preflight_reuses_existing_audited_state_without_new_baseline \
  -q
```

Expected: the acceptance test fails with `acceptance must not build an external notifier`; the default-deployment assertion passes.

- [ ] **Step 3: Select the notifier from the existing actor**

In the `trend-drawdown-preflight` call in `src/open_trader/cli.py`, replace:

```python
notifier=build_notifier(config),
```

with:

```python
notifier=(
    NullNotifier()
    if args.actor == "acceptance"
    else build_notifier(config)
),
```

Do not change the parser default, the `actor` passed into the drawdown audit event, or any exception/exit-code handling.

- [ ] **Step 4: Run the routing tests and verify they pass**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_strategy_drawdown_cli.py::test_trend_drawdown_preflight_cli_bootstraps_all_markets_independently \
  tests/test_strategy_drawdown_cli.py::test_trend_drawdown_preflight_reuses_existing_audited_state_without_new_baseline \
  -q
```

Expected: `2 passed`.

- [ ] **Step 5: Commit the isolated routing change**

```bash
git add src/open_trader/cli.py tests/test_strategy_drawdown_cli.py
git diff --cached --check
git commit -m "fix: silence drawdown alerts during acceptance"
```

---

### Task 2: Batch and Translate Drawdown Failure Alerts

**Files:**
- Modify: `tests/test_drawdown_preflight.py:10-58`
- Modify: `tests/test_drawdown_preflight.py:186-224`
- Modify: `tests/test_drawdown_preflight.py:407-453`
- Modify: `src/open_trader/drawdown_preflight.py:21-35`
- Modify: `src/open_trader/drawdown_preflight.py:265-324`

**Interfaces:**
- Consumes: per-market result dictionaries containing `market`, `status`, optional `failure_status`, and the strategy version in `market_inputs`.
- Produces: one `notifier.notify(title: str, message: str)` call for all newly active failures and the same JSON ledger schema `{"active": list[str]}`.

- [ ] **Step 1: Let the existing preflight test helper accept a recording notifier**

Change the notification import in `tests/test_drawdown_preflight.py` to:

```python
from open_trader.notifications import Notifier, NullNotifier
```

Change `run_preflight` to:

```python
def run_preflight(
    root: Path,
    inputs: dict[str, DrawdownMarketInput],
    *,
    notifier: Notifier | None = None,
) -> dict[str, object]:
    return run_drawdown_preflight(
        data_dir=root / "data",
        reports_dir=root / "reports",
        market_inputs=inputs,
        accepted_git_sha="a" * 40,
        actor="acceptance",
        occurred_at="2026-07-20T08:00:00+08:00",
        notifier=notifier if notifier is not None else NullNotifier(),
    )
```

- [ ] **Step 2: Tighten the unknown-error regression assertion**

In `test_missing_approved_predecessor_fails_closed_without_writing_state`, create a recorder and pass it to the helper:

```python
notifier = RecordingNotifier()
result = run_preflight(
    tmp_path,
    {"CN": target},
    notifier=notifier,
)
```

Keep the existing fail-closed and unchanged-state assertions, then add:

```python
assert notifier.calls == [(
    "【需处理｜系统｜累计回撤状态阻断】",
    "\n".join([
        "发生：新策略版本无法建立或继承累计回撤状态",
        "影响：CN v9 暂停新开仓；卖出和保护线继续运行",
        "现在做：让 Codex 检查回撤预检并重新部署；不要手动解除限制",
        "",
        "明细：",
        "- CN v9：回撤预检失败",
    ]),
)]
assert "approved predecessor drawdown state is unavailable" not in notifier.calls[0][1]
```

This exercises the real `preflight_failed` path without manufacturing a fake exception.

- [ ] **Step 3: Replace the single-market alert test with the grouped contract**

Replace `test_failure_alert_is_deduplicated_and_rearmed_after_recovery` with:

```python
def test_failure_alert_is_grouped_deduplicated_and_rearmed_after_recovery(
    tmp_path: Path,
) -> None:
    failed_inputs = {
        market: replace(market_input(market), baseline_equity=None)
        for market in ("CN", "HK", "US")
    }
    notifier = RecordingNotifier()
    request = dict(
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        market_inputs=failed_inputs,
        accepted_git_sha="a" * 40,
        actor="deployment",
        occurred_at="2026-07-20T08:00:00+08:00",
        notifier=notifier,
    )

    assert run_drawdown_preflight(**request)["status"] == "failed"
    assert run_drawdown_preflight(**request)["status"] == "failed"
    expected = (
        "【需处理｜系统｜累计回撤状态阻断】",
        "\n".join([
            "发生：新策略版本无法建立或继承累计回撤状态",
            "影响：CN v4、HK v4、US v4 暂停新开仓；卖出和保护线继续运行",
            "现在做：让 Codex 检查回撤预检并重新部署；不要手动解除限制",
            "",
            "明细：",
            "- CN v4：历史基线不可用",
            "- HK v4：历史基线不可用",
            "- US v4：历史基线不可用",
        ]),
    )
    assert notifier.calls == [expected]
    assert json.loads(
        (tmp_path / "data/trend_drawdown/alerts.json").read_text()
    )["active"] == [
        "CN|v4|baseline_unavailable",
        "HK|v4|baseline_unavailable",
        "US|v4|baseline_unavailable",
    ]

    request["market_inputs"] = {
        market: market_input(market) for market in ("CN", "HK", "US")
    }
    assert run_drawdown_preflight(**request)["status"] == "ready"

    state_root = tmp_path / "data/trend_drawdown"
    (state_root / "state.json").unlink()
    shutil.rmtree(state_root / "snapshots")
    request["market_inputs"] = failed_inputs

    assert run_drawdown_preflight(**request)["status"] == "failed"
    assert notifier.calls == [expected, expected]
```

- [ ] **Step 4: Make notification failure assert one batched attempt**

Replace `test_notification_failure_does_not_change_fail_closed_result` with:

```python
def test_notification_failure_does_not_change_fail_closed_result(
    tmp_path: Path,
) -> None:
    notifier = RecordingNotifier(fail=True)
    result = run_drawdown_preflight(
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        market_inputs={
            market: replace(market_input(market), baseline_equity=None)
            for market in ("CN", "HK", "US")
        },
        accepted_git_sha="a" * 40,
        actor="deployment",
        occurred_at="2026-07-20T08:00:00+08:00",
        notifier=notifier,
    )

    assert result["status"] == "failed"
    assert len(notifier.calls) == 1
    assert not (tmp_path / "data/trend_drawdown/alerts.json").exists()
```

- [ ] **Step 5: Run the notification tests and verify they fail for copy and fan-out**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_drawdown_preflight.py::test_missing_approved_predecessor_fails_closed_without_writing_state \
  tests/test_drawdown_preflight.py::test_failure_alert_is_grouped_deduplicated_and_rearmed_after_recovery \
  tests/test_drawdown_preflight.py::test_notification_failure_does_not_change_fail_closed_result \
  -q
```

Expected: all three tests fail against the old implementation because it uses the old title/raw English, sends one call per market, or retries each market independently.

- [ ] **Step 6: Add the stable Chinese failure labels**

Immediately after `APPROVED_DRAWDOWN_PREDECESSORS` in `src/open_trader/drawdown_preflight.py`, add:

```python
_DRAWDOWN_FAILURE_LABELS = {
    "baseline_unavailable": "历史基线不可用",
    "parameter_mismatch": "策略参数与已登记版本不一致",
    "parameter_identity_missing": "策略参数身份缺失",
    "state_missing_recovery_failed": "回撤状态丢失且恢复失败",
    "state_corrupt_recovery_failed": "回撤状态损坏且恢复失败",
}
```

Do not include raw exception text in this mapping or add configuration for the labels.

- [ ] **Step 7: Replace per-market sends with one pending batch**

In `_sync_failure_alerts`, keep the existing ledger read and atomic write. Replace the per-result notify behavior from `original = set(active)` through the end of the result loop with:

```python
original = set(active)
pending: list[tuple[str, str, str]] = []
for result in results:
    market = str(result["market"])
    strategy = market_inputs[market].strategy_snapshot
    version = str(strategy.get("strategy_version") or "")
    prefix = f"{market}|{version}|"
    if result["status"] in {"ready", "bootstrapped", "recovered"}:
        active = {key for key in active if not key.startswith(prefix)}
        continue
    failure_status = result.get("failure_status")
    if result["status"] != "failed" or not failure_status:
        continue
    failure_status = str(failure_status)
    key = prefix + failure_status
    if key in active:
        continue
    pending.append((
        key,
        f"{market} {version}",
        _DRAWDOWN_FAILURE_LABELS.get(failure_status, "回撤预检失败"),
    ))

if pending:
    affected = "、".join(label for _, label, _ in pending)
    details = "\n".join(
        f"- {label}：{reason}" for _, label, reason in pending
    )
    try:
        notifier.notify(
            "【需处理｜系统｜累计回撤状态阻断】",
            "\n".join([
                "发生：新策略版本无法建立或继承累计回撤状态",
                (
                    f"影响：{affected} 暂停新开仓；"
                    "卖出和保护线继续运行"
                ),
                (
                    "现在做：让 Codex 检查回撤预检并重新部署；"
                    "不要手动解除限制"
                ),
                "",
                "明细：",
                details,
            ]),
        )
    except Exception:
        pass
    else:
        active.update(key for key, _, _ in pending)
```

Leave the existing `if active == original`, JSON serialization, temporary file, atomic replace, and cleanup code unchanged.

- [ ] **Step 8: Run the notification tests and verify they pass**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_drawdown_preflight.py::test_missing_approved_predecessor_fails_closed_without_writing_state \
  tests/test_drawdown_preflight.py::test_failure_alert_is_grouped_deduplicated_and_rearmed_after_recovery \
  tests/test_drawdown_preflight.py::test_notification_failure_does_not_change_fail_closed_result \
  -q
```

Expected: `3 passed`.

- [ ] **Step 9: Run the complete focused preflight suites**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_drawdown_preflight.py \
  tests/test_strategy_drawdown_cli.py \
  -q
```

Expected: `26 passed`.

- [ ] **Step 10: Commit the grouped notification behavior**

```bash
git add src/open_trader/drawdown_preflight.py tests/test_drawdown_preflight.py
git diff --cached --check
git commit -m "fix: consolidate drawdown failure alerts"
```

---

### Task 3: Verify the Real Workflow and Record the Operator Change

**Files:**
- Modify: `CHANGELOG.md:1-8`

**Interfaces:**
- Consumes: the completed CLI routing and grouped alert behavior from Tasks 1 and 2.
- Produces: direct workflow evidence, full regression evidence, process-manager evidence, and the dated merge-log entry required before any later merge to `main`.

- [ ] **Step 1: Re-run the focused tests from the implementation worktree**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  tests/test_drawdown_preflight.py \
  tests/test_strategy_drawdown_cli.py \
  -q
```

Expected: `26 passed`.

- [ ] **Step 2: Run the real acceptance-actor preflight directly**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m open_trader \
  trend-drawdown-preflight \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env \
  --repo /Users/ray/projects/open_trader/.worktrees/reduce-drawdown-alert-noise \
  --actor acceptance
```

Expected: exit `0`, JSON result status `ready`, and no external notification because the CLI passes `NullNotifier`. If the command exits `1` or `2`, stop and diagnose the live preflight; do not replace the check with fixtures or mocks.

- [ ] **Step 3: Confirm no long-running process can retain the old preflight code**

Run:

```bash
ps -axo pid,lstart,command | rg '[o]pen_trader.*trend-drawdown-preflight' || true
launchctl list | rg 'open.?trader|trend' || true
screen -ls || true
rg -n "trend-drawdown-preflight" /Users/ray/Library/LaunchAgents \
  /Users/ray/projects/open_trader 2>/dev/null \
  --glob '*.plist' --glob '*.sh' --glob 'Makefile'
```

Expected: no resident `trend-drawdown-preflight` process. The command is started per acceptance run, so unrelated trend controllers must not be restarted. If a resident preflight process exists and its working directory is not this worktree, stop it, rerun the direct command, and capture the new PID/timestamp/log evidence before reporting live behavior.

- [ ] **Step 4: Run the full automated suite with repository data**

Run from `/Users/ray/projects/open_trader`:

```bash
PYTHONSAFEPATH=1 \
PYTHONPATH="/Users/ray/projects/open_trader/.worktrees/reduce-drawdown-alert-noise:/Users/ray/projects/open_trader/.worktrees/reduce-drawdown-alert-noise/src" \
/Users/ray/projects/open_trader/.venv/bin/python -m pytest \
  /Users/ray/projects/open_trader/.worktrees/reduce-drawdown-alert-noise/tests \
  -q
```

Expected: all tests pass with zero failures. The repository-root working directory is required because six legacy-snapshot tests read ignored historical data under `/Users/ray/projects/open_trader/data`.

- [ ] **Step 5: Add the dated changelog entry**

Insert this section above `## 2026-07-25` in `CHANGELOG.md`:

```markdown
## 2026-07-26

- Silenced external cumulative-drawdown alerts during deployment acceptance and
  consolidated real multi-market failures into one actionable Chinese message
  without weakening fail-closed entry controls. Verified focused/full tests and
  the live acceptance-actor preflight.
```

- [ ] **Step 6: Commit the verified operator log**

```bash
git add CHANGELOG.md
git diff --cached --check
git commit -m "docs: log drawdown alert noise reduction"
```

- [ ] **Step 7: Perform the final clean-tree evidence check**

Run:

```bash
git status --short --branch
git log -4 --oneline --decorate
git diff main...HEAD --check
git diff main...HEAD --stat
```

Expected: clean branch `fix/reduce-drawdown-alert-noise`; commits for the design, acceptance routing, grouped alert behavior, and changelog; no whitespace errors; changes limited to the design/plan docs, `CHANGELOG.md`, two source files, and two test files.

Do not merge to `main`, push, or send a real failure notification as part of this plan. Hand the verified branch back for user review.
