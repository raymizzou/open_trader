# Retire TradingAgents Daily Chain Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove TradingAgents/DeepSeek from scheduled deployment, the current Dashboard flow, and acceptance so the existing trend controllers can deploy and run without an AI balance.

**Architecture:** Keep the manual TradingAgents implementation and historical files untouched. Make the existing launchd installer controller-only, remove the old AI-backed holding detail entry point, and delete acceptance checks that consume daily AI artifacts; the trend controllers and their execution safeguards remain authoritative.

**Tech Stack:** Bash, Python 3, pytest, vanilla JavaScript, launchd, existing open_trader CLI.

## Global Constraints

- Do not add a feature flag, replacement LLM, dependency, migration, or backfill.
- Do not delete historical TradingAgents artifacts or manual TradingAgents commands.
- Do not change trend signals, sizing, protection, executor-host fencing, or duplicate prevention.
- Missing or failed trend reports remain blocking and retryable.
- `make acceptance` is the final gate; only PASS permits deployment completion.
- After PASS, redeploy the exact accepted SHA and verify PID, working directory, Git SHA, fresh logs, and HTTP 200.

---

### Task 1: Make deployment controller-only

**Files:**
- Modify: `scripts/install_daily_premarket_launchd.sh`
- Test: `tests/test_daily_premarket.py`

**Interfaces:**
- Consumes: existing `stop_label`, `verify_absent`, `render_controller`, and executor-host selection in `install_daily_premarket_launchd.sh`.
- Produces: the existing installer command, now always retiring `com.open-trader.premarket`, `.hk`, and `.us` and installing only selected trend controllers on the executor host.

- [ ] **Step 1: Replace the ordinary-job expectation with a failing controller-only test**

Update `test_launchd_installer_default_renders_hk_and_us_jobs` so its config contains the local executor host and it expects exactly the three controller labels:

```python
def test_launchd_installer_default_renders_only_three_controllers(tmp_path: Path) -> None:
    repo = _copy_launchd_installer_assets(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    (repo / "config/daily_premarket.env").write_text("\n".join([
        f"OPEN_TRADER_REPO={repo}",
        "OPEN_TRADER_PYTHON=.venv/bin/python",
        f"OPEN_TRADER_TREND_EXECUTOR_HOST={_local_hostname()}",
    ]), encoding="utf-8")

    result = subprocess.run(
        [str(repo / "scripts/install_daily_premarket_launchd.sh"), "--dry-run"],
        check=True, capture_output=True, encoding="utf-8",
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
    )

    labels = {payload["Label"] for payload in _launchd_plists(result.stdout)}
    assert labels == {
        "com.open-trader.trend-market-controller.cn",
        "com.open-trader.trend-market-controller.hk",
        "com.open-trader.trend-market-controller.us",
    }
    assert "com.open-trader.premarket" not in result.stdout
```

Add a real-mode read-only test that pre-creates all three retired plist paths and
proves cleanup without starting a controller:

```python
def test_launchd_installer_retires_all_premarket_jobs(tmp_path: Path) -> None:
    repo = _copy_launchd_installer_assets(tmp_path)
    home = tmp_path / "home"
    agents = home / "Library/LaunchAgents"
    agents.mkdir(parents=True)
    labels = (
        "com.open-trader.premarket",
        "com.open-trader.premarket.hk",
        "com.open-trader.premarket.us",
    )
    for label in labels:
        (agents / f"{label}.plist").write_text("retired\n", encoding="utf-8")
    fake_bin = _fake_launchctl_bin(tmp_path)
    (repo / "config/daily_premarket.env").write_text("\n".join([
        f"OPEN_TRADER_REPO={repo}",
        "OPEN_TRADER_PYTHON=.venv/bin/python",
        "OPEN_TRADER_TREND_EXECUTOR_HOST=another-host",
    ]), encoding="utf-8")

    result = subprocess.run(
        [str(repo / "scripts/install_daily_premarket_launchd.sh"), "--market", "all"],
        check=True, capture_output=True, encoding="utf-8",
        env={"HOME": str(home), "PATH": f"{fake_bin}:/usr/bin:/bin"},
    )

    assert "effective mode: readonly" in result.stdout
    assert all(not (agents / f"{label}.plist").exists() for label in labels)
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py -k 'default_renders_only_three_controllers or retires_all_premarket_jobs' -q
```

Expected: FAIL because the default installer still renders ordinary HK/US premarket jobs and does not remove every retired premarket label.

- [ ] **Step 3: Delete the ordinary install branch and retire its labels**

Keep `--trend-only` accepted as a compatibility no-op, delete `render_premarket` and the `TREND_ONLY=0` ordinary-job branch, and add this existing-helper loop after the dry-run exit and before controller cleanup:

```bash
for label in \
  "com.open-trader.premarket" \
  "com.open-trader.premarket.hk" \
  "com.open-trader.premarket.us"
do
  stop_label "$label"
  verify_absent "$label"
done
```

Default and `--trend-only` invocation then share the existing controller-only path. Do not delete the manual CLI or historical plist template.

- [ ] **Step 4: Run the installer test slice and confirm GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py -k 'launchd_installer or launchd_uninstaller or launchd_template' -q
```

Expected: all selected tests PASS after obsolete ordinary-install assertions are removed or changed to controller-only behavior.

- [ ] **Step 5: Commit**

```bash
git add scripts/install_daily_premarket_launchd.sh tests/test_daily_premarket.py
git commit -m "fix: retire scheduled TradingAgents jobs"
```

---

### Task 2: Remove the AI-backed current Dashboard and acceptance dependency

**Files:**
- Modify: `src/open_trader/dashboard_static/dashboard.js`
- Modify: `src/open_trader/dashboard_acceptance.py`
- Test: `tests/test_dashboard_web.py`
- Test: `tests/test_dashboard_acceptance.py`

**Interfaces:**
- Consumes: existing trend report account views, T-signal detail, Dashboard payload, and acceptance browser flow.
- Produces: account holdings with only the existing `做T` detail action, plus acceptance that validates trend/runtime/broker/browser facts without daily AI artifacts.

- [ ] **Step 1: Write failing Dashboard retirement tests**

Change the account-row JavaScript test to require `做T` while rejecting the old decision entry point:

```javascript
const html = elements["holdings-body"].innerHTML;
if (!html.includes(">做T<")) throw new Error(html);
for (const retired of ["data-detail-mode=\"decision\"", "TradingAgents", "交易决策"]) {
  if (html.includes(retired)) throw new Error(`retired UI remains: ${retired}`);
}
```

Add a payload acceptance test proving absent daily AI fields are not errors:

```python
def test_dashboard_acceptance_does_not_require_daily_ai_sources() -> None:
    payload = valid_payload()
    for holding in payload["holdings"]:
        for key in (
            "agent_report", "tradingagents_summary", "technical_facts",
            "decision_facts", "futu_skill_facts",
        ):
            holding.pop(key, None)

    assert validate_dashboard_payload(payload, expected_cn=5) == []
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py tests/test_dashboard_acceptance.py -k 'account_holding or daily_ai_sources or decision_tabs or external_source' -q
```

Expected: FAIL because the UI still renders `交易决策` and acceptance still requires TradingAgents/DeepSeek-derived sources.

- [ ] **Step 3: Remove the current AI-backed UI entry point**

In `renderAccountHoldingRow`, retain only the existing T-signal button for non-simulated holdings:

```javascript
const detailActions = simulated
  ? ""
  : `<button class="${escapeHtml(tSignalButtonClass(holding))}" type="button" data-detail-key="${escapeHtml(row.key)}" data-detail-mode="t_signal">做T</button>`;
```

Do not add a disabled state or replacement report. Existing trend report and history views remain unchanged.

- [ ] **Step 4: Delete AI source gating from acceptance**

Delete `REQUIRED_SOURCE_PATHS`, `BALANCE_CAUSAL_SOURCES`, the daily-source loop in `validate_dashboard_payload`, `_partition_external_source_errors`, `_check_decision_tabs`, and its browser invocation. Change `_first_in_scope_holding` to select the first holding with a valid account broker without consulting `agent_report`:

```python
def _first_in_scope_holding(payload: dict[str, Any]) -> tuple[str, str, str]:
    for holding in payload.get("holdings") or []:
        brokers = {
            "phillips" if value == "phillip" else value
            for value in [
                *str(holding.get("brokers") or "").lower().split(";"),
                str(holding.get("broker") or "").lower(),
                *(
                    str(detail.get("broker") or "").lower()
                    for detail in holding.get("broker_details") or []
                    if isinstance(detail, Mapping)
                ),
            ]
            if value
        }
        broker = next((item for item in ACCOUNT_BROKERS if item in brokers), "")
        if broker:
            return str(holding.get("market", "")), str(holding.get("symbol", "")), broker
    raise AssertionError("no account holding exists in Dashboard payload")
```

Remove the now-obsolete DeepSeek blocker tests and decision-tab browser tests. Keep `classify_result`'s generic external-blocker support because other external acceptance blockers still use it.

- [ ] **Step 5: Run focused Dashboard tests and confirm GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_dashboard_web.py tests/test_dashboard_acceptance.py tests/test_dashboard.py -q
```

Expected: all selected tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/open_trader/dashboard_static/dashboard.js src/open_trader/dashboard_acceptance.py tests/test_dashboard_web.py tests/test_dashboard_acceptance.py
git commit -m "fix: remove TradingAgents deployment blocker"
```

---

### Task 3: Verify and deploy the accepted SHA

**Files:**
- No source changes.

**Interfaces:**
- Consumes: the controller-only installer, Dashboard acceptance command, current executor-host configuration, and launchd.
- Produces: a PASS acceptance result and live CN/HK/US controller plus Dashboard processes running the exact accepted SHA.

- [ ] **Step 1: Run the relevant automated tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_daily_premarket.py tests/test_dashboard.py tests/test_dashboard_web.py tests/test_dashboard_acceptance.py -q
```

Expected: PASS with zero failures.

- [ ] **Step 2: Run the final acceptance gate**

Run only after all source commits are complete:

```bash
make acceptance
```

Expected: final output contains `PASS`; `FAIL` must be fixed and rerun, while `BLOCKED` must be reported without claiming completion.

- [ ] **Step 3: Capture and redeploy the accepted SHA**

Run:

```bash
git rev-parse HEAD
scripts/install_daily_premarket_launchd.sh --market all
```

Expected: the installer reports the local host as `execute`, removes all retired premarket labels, and verifies fresh CN/HK/US controller PIDs.

- [ ] **Step 4: Verify no retired job or process remains**

Run:

```bash
launchctl print gui/$(id -u)/com.open-trader.premarket
launchctl print gui/$(id -u)/com.open-trader.premarket.hk
launchctl print gui/$(id -u)/com.open-trader.premarket.us
pgrep -af 'open_trader .*run-daily-premarket'
```

Expected: every launchctl lookup reports no service and `pgrep` returns no matching TradingAgents daily process.

- [ ] **Step 5: Verify fresh controller and Dashboard runtime evidence**

For CN, HK, and US, inspect `data/trend_controller/<MARKET>/status.json` and the matching fresh launchd log. Confirm each status has the accepted `git_sha`, a live `pid`, the accepted worktree `working_directory`, and a fresh `heartbeat_at`. Restart the Dashboard from the same SHA, then run:

```bash
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/
```

Expected: `200`, with the Dashboard PID/cwd/SHA and fresh log matching the accepted deployment.
