# Drawdown Preflight Skipped Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `make acceptance` continue when the completed-date frozen drawdown baseline is genuinely absent, while preserving `BLOCKED` for external dependency failures and `FAIL` for invalid data.

**Architecture:** Move frozen-baseline lookup and classification behind the existing `run_drawdown_preflight` seam, after audited-state recovery and predecessor checks. Return an explicit local lookup result (`available`, `missing`, or `invalid`); translate only `missing` into a visible per-market `skipped` result whose overall CLI exit is successful. Keep `Makefile` free of status parsing and keep runtime entry protection fail-closed.

**Tech Stack:** Python 3.12, dataclasses, `Decimal`, pytest, Make, existing Dashboard acceptance runner.

## Global Constraints

- Do not change the 5% cumulative-drawdown rule.
- Do not synthesize a historical baseline from live or partial account values.
- Do not change strategy versions, approved predecessor inheritance, parameter identity, notifications, or controller entry checks.
- Missing baseline is the only skippable case.
- Futu/trading-calendar failures remain `unavailable` and exit `2`.
- Malformed or inconsistent completed-date data remains `failed` and exit `1`.
- A skipped market must not create or modify its drawdown state.
- Keep one public `make acceptance` workflow; do not add dependencies or general result abstractions.
- Run `make acceptance` only after implementation, focused tests, full tests, changelog, commits, direct workflow checks, and live Dashboard deployment are complete.
- Only `make acceptance` `PASS` permits review handoff; then redeploy the exact accepted Git SHA and verify PID, working directory, SHA, fresh logs, and HTTP 200.

---

## File Map

- `src/open_trader/drawdown_preflight.py`: owns frozen-baseline classification, audited-state recovery ordering, per-market `skipped` results, and overall result aggregation.
- `src/open_trader/cli.py`: obtains only Futu calendar dates and effective strategy snapshots, then delegates baseline handling to the preflight module.
- `tests/test_drawdown_preflight.py`: proves available/missing/invalid baseline classification, state recovery ordering, skip aggregation, and no state mutation.
- `tests/test_strategy_drawdown_cli.py`: proves CLI exit semantics and preserves external dependency blocking.
- `CHANGELOG.md`: records the operator-visible acceptance behavior.

### Task 1: Classify frozen baseline lookup outcomes

**Files:**

- Modify: `src/open_trader/drawdown_preflight.py:3-117`
- Test: `tests/test_drawdown_preflight.py:1-45,378-405`

**Interfaces:**

- Produces:

  ```python
  @dataclass(frozen=True)
  class FrozenBaselineLookup:
      status: Literal["available", "missing", "invalid"]
      equity: Decimal | None = None
      error: str = ""

  def load_frozen_baseline(
      reports_dir: Path,
      *,
      market: str,
      strategy_id: str,
      strategy_version: str,
      source_date: str,
  ) -> FrozenBaselineLookup
  ```

- Replaces: `frozen_missing_baseline(...) -> Decimal | None`.
- Consumers: Task 2 calls `load_frozen_baseline` only when no audited current state or approved predecessor can satisfy the market.

- [ ] **Step 1: Replace the old loader test with explicit outcome tests**

Update the import in `tests/test_drawdown_preflight.py` to
`FrozenBaselineLookup, load_frozen_baseline`. Rename the existing loader test
and assert:

```python
def test_load_frozen_baseline_returns_original_account_equity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reports/trend_us_tiger/2026-07-17-r2.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({
            "metadata": {"market": "US"},
            "strategy_snapshot": {
                "strategy_id": "trend_animals_warm_to_hot/US/v4",
                "strategy_version": "v4",
            },
            "account": {
                "source_date": "2026-07-17",
                "net_value": "123.45",
            },
            "drawdown_summary": {"state_status": "missing"},
        }),
        encoding="utf-8",
    )

    result = load_frozen_baseline(
        tmp_path / "reports",
        market="US",
        strategy_id="trend_animals_warm_to_hot/US/v4",
        strategy_version="v4",
        source_date="2026-07-17",
    )

    assert result == FrozenBaselineLookup(
        status="available",
        equity=Decimal("123.45"),
    )
```

Add a readable older-version report for the completed date and prove it is
absence, not corruption:

```python
def test_load_frozen_baseline_reports_missing_current_strategy(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reports/trend_us_tiger/2026-07-17.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({
            "metadata": {"market": "US"},
            "strategy_snapshot": {
                "strategy_id": "trend_animals_warm_to_hot/US/v4",
                "strategy_version": "v4",
            },
            "account": {
                "source_date": "2026-07-17",
                "net_value": "123.45",
            },
            "drawdown_summary": {"state_status": "ok"},
        }),
        encoding="utf-8",
    )

    result = load_frozen_baseline(
        tmp_path / "reports",
        market="US",
        strategy_id="trend_animals_warm_to_hot/US/v5",
        strategy_version="v5",
        source_date="2026-07-17",
    )

    assert result.status == "missing"
    assert result.equity is None
    assert result.error == ""
```

Add invalid completed-date cases:

```python
@pytest.mark.parametrize(
    ("content", "error_text"),
    [
        ("{", "unreadable frozen drawdown baseline"),
        (
            json.dumps({
                "metadata": {"market": "US"},
                "strategy_snapshot": {
                    "strategy_id": "trend_animals_warm_to_hot/US/v4",
                    "strategy_version": "v4",
                },
                "account": {
                    "source_date": "2026-07-17",
                    "net_value": "not-a-number",
                },
                "drawdown_summary": {"state_status": "missing"},
            }),
            "invalid frozen drawdown baseline",
        ),
    ],
)
def test_load_frozen_baseline_rejects_invalid_completed_date_artifacts(
    tmp_path: Path,
    content: str,
    error_text: str,
) -> None:
    path = tmp_path / "reports/trend_us_tiger/2026-07-17.json"
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")

    result = load_frozen_baseline(
        tmp_path / "reports",
        market="US",
        strategy_id="trend_animals_warm_to_hot/US/v4",
        strategy_version="v4",
        source_date="2026-07-17",
    )

    assert result.status == "invalid"
    assert error_text in result.error
```

- [ ] **Step 2: Run the loader tests and verify red**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_drawdown_preflight.py::test_load_frozen_baseline_returns_original_account_equity \
  tests/test_drawdown_preflight.py::test_load_frozen_baseline_reports_missing_current_strategy \
  tests/test_drawdown_preflight.py::test_load_frozen_baseline_rejects_invalid_completed_date_artifacts \
  -q
```

Expected: FAIL because `FrozenBaselineLookup` and `load_frozen_baseline` do not
exist and malformed files are currently ignored.

- [ ] **Step 3: Implement the local lookup result and completed-date selection**

In `src/open_trader/drawdown_preflight.py`, import `Literal` and add:

```python
from typing import Literal


@dataclass(frozen=True)
class FrozenBaselineLookup:
    status: Literal["available", "missing", "invalid"]
    equity: Decimal | None = None
    error: str = ""
```

Replace `frozen_missing_baseline` with `load_frozen_baseline`. Restrict candidates
to the requested completed date:

```python
directory = reports_dir / REPORT_DIRECTORIES[market]
candidates = [
    path
    for path in (
        directory / f"{source_date}.json",
        *directory.glob(f"{source_date}-r*.json"),
    )
    if path.is_file()
]
if not candidates:
    return FrozenBaselineLookup(status="missing")
```

For candidates in reverse filename order:

1. return `invalid` when the JSON or required top-level market/strategy
   structure is unreadable;
2. continue when a well-formed report belongs to another strategy identity or
   version;
3. for a matching strategy, require the exact source date, a positive finite
   `account.net_value`, and `drawdown_summary.state_status == "missing"`;
4. return `available` with the parsed `Decimal`;
5. return `missing` if all readable reports belong to other strategy versions.

Use stable error messages containing the path:

```python
return FrozenBaselineLookup(
    status="invalid",
    error=f"invalid frozen drawdown baseline: {path}",
)
```

- [ ] **Step 4: Run loader tests and the full preflight module**

Run:

```bash
.venv/bin/python -m pytest tests/test_drawdown_preflight.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit the classified loader**

```bash
git add src/open_trader/drawdown_preflight.py tests/test_drawdown_preflight.py
git commit -m "refactor: classify frozen drawdown baselines"
```

### Task 2: Skip only genuinely missing baselines after recovery

**Files:**

- Modify: `src/open_trader/drawdown_preflight.py:120-269`
- Test: `tests/test_drawdown_preflight.py:50-365`

**Interfaces:**

- Consumes: `load_frozen_baseline(...) -> FrozenBaselineLookup`.
- Preserves:

  ```python
  def run_drawdown_preflight(
      *,
      data_dir: Path,
      reports_dir: Path,
      market_inputs: Mapping[str, DrawdownMarketInput],
      accepted_git_sha: str,
      actor: str,
      occurred_at: str,
      notifier: Notifier,
  ) -> dict[str, object]
  ```

- Produces per-market missing result:

  ```python
  {
      "market": market,
      "status": "skipped",
      "reason": "baseline_missing",
      "source_date": item.source_date,
  }
  ```

- Produces overall `"status": "ready"` when all market statuses are
  `ready`, `bootstrapped`, `recovered`, or `skipped`.

- [ ] **Step 1: Write failing skip and invalid-data tests**

Replace `test_first_activation_without_baseline_fails_closed` with:

```python
def test_first_activation_without_matching_baseline_is_skipped(
    tmp_path: Path,
) -> None:
    target = replace(market_input("CN"), baseline_equity=None)

    result = run_preflight(tmp_path, {"CN": target})

    assert result == {
        "status": "ready",
        "markets": [{
            "market": "CN",
            "status": "skipped",
            "reason": "baseline_missing",
            "source_date": "2026-07-17",
        }],
    }
    assert not (tmp_path / "data/trend_drawdown/state.json").exists()
```

Add mixed aggregation:

```python
def test_skipped_market_does_not_block_other_market_bootstrap(
    tmp_path: Path,
) -> None:
    result = run_preflight(
        tmp_path,
        {
            "CN": replace(market_input("CN"), baseline_equity=None),
            "US": market_input("US"),
        },
    )

    assert result["status"] == "ready"
    assert [item["status"] for item in result["markets"]] == [
        "skipped", "bootstrapped"
    ]
    state = json.loads(
        (tmp_path / "data/trend_drawdown/state.json").read_text(encoding="utf-8")
    )
    assert [record["market"] for record in state["records"]] == ["US"]
```

Add invalid matching data:

```python
def test_invalid_matching_baseline_fails(tmp_path: Path) -> None:
    path = tmp_path / "reports/trend_a_share/2026-07-17.json"
    path.parent.mkdir(parents=True)
    path.write_text("{", encoding="utf-8")

    result = run_preflight(
        tmp_path,
        {"CN": replace(market_input("CN"), baseline_equity=None)},
    )

    assert result["status"] == "failed"
    assert result["markets"][0]["failure_status"] == "baseline_invalid"
    assert not (tmp_path / "data/trend_drawdown/state.json").exists()
```

- [ ] **Step 2: Run the new preflight tests and verify red**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_drawdown_preflight.py::test_first_activation_without_matching_baseline_is_skipped \
  tests/test_drawdown_preflight.py::test_skipped_market_does_not_block_other_market_bootstrap \
  tests/test_drawdown_preflight.py::test_invalid_matching_baseline_fails \
  -q
```

Expected: FAIL because missing input baselines still produce
`baseline_unavailable` failures and the loader is not called by the preflight.

- [ ] **Step 3: Resolve state and predecessor paths before baseline lookup**

Inside `run_drawdown_preflight`, retain state recovery and `existing_keys`
before entering baseline classification. After computing `was_present`,
`reason`, and `approved_predecessor`, use a local baseline:

```python
baseline_equity = item.baseline_equity
if (
    not was_present
    and baseline_equity is None
    and not approved_predecessor
):
    if item.source_date is None or item.entry_eligible_from is None:
        results.append({
            "market": market,
            "status": "failed",
            "failure_status": "baseline_unavailable",
            "error": "completed-date frozen Futu baseline date is unavailable",
        })
        continue
    baseline = load_frozen_baseline(
        reports_dir,
        market=market,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        source_date=item.source_date,
    )
    if baseline.status == "missing":
        results.append({
            "market": market,
            "status": "skipped",
            "reason": "baseline_missing",
            "source_date": item.source_date,
        })
        continue
    if baseline.status == "invalid":
        results.append({
            "market": market,
            "status": "failed",
            "failure_status": "baseline_invalid",
            "error": baseline.error,
        })
        continue
    baseline_equity = baseline.equity
```

Pass `baseline_equity`, not `item.baseline_equity`, to
`automatic_bootstrap_strategy_drawdown`.

Keep the overall aggregation unchanged:

```python
overall = (
    "failed" if "failed" in statuses
    else "unavailable" if "unavailable" in statuses
    else "ready"
)
```

This intentionally treats any collection of successful and skipped markets as
overall ready while preserving each skip in `markets`.

- [ ] **Step 4: Run preflight tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_drawdown_preflight.py -q
```

Expected: all tests pass, including snapshot recovery, predecessor inheritance,
failure alerts, and the new skipped-market cases.

- [ ] **Step 5: Commit missing-baseline skip semantics**

```bash
git add src/open_trader/drawdown_preflight.py tests/test_drawdown_preflight.py
git commit -m "fix: skip drawdown preflight without a baseline"
```

### Task 3: Delegate CLI baseline handling and preserve real blockers

**Files:**

- Modify: `src/open_trader/cli.py:130-145,1359-1447`
- Test: `tests/test_strategy_drawdown_cli.py:156-500`

**Interfaces:**

- Consumes: unchanged `DrawdownMarketInput` and `run_drawdown_preflight`.
- Removes CLI use of `strategy_drawdown_state_status` and
  `load_frozen_baseline`; the CLI supplies `baseline_equity=None`.
- Preserves CLI result mapping:

  ```python
  {"ready": 0, "failed": 1, "unavailable": 2}
  ```

- [ ] **Step 1: Change the missing-baseline CLI test to expect a visible skip**

Rename
`test_trend_drawdown_preflight_does_not_relabel_live_nav_as_historical` to
`test_trend_drawdown_preflight_skips_missing_frozen_baseline_without_live_nav`
and assert:

```python
result = cli.main([
    "trend-drawdown-preflight",
    "--config", str(tmp_path / "daily.env"),
    "--repo", str(tmp_path),
])

assert result == 0
output = json.loads(capsys.readouterr().out)
assert output["status"] == "ready"
assert [item["status"] for item in output["markets"]] == [
    "skipped", "skipped", "skipped"
]
assert {
    item["reason"] for item in output["markets"]
} == {"baseline_missing"}
assert account_calls == []
assert not (config.data_dir / "trend_drawdown/state.json").exists()
```

- [ ] **Step 2: Add a Futu calendar failure test**

Use a quote adapter whose calendar call fails:

```python
class UnavailableQuote:
    def __init__(self, **_: object) -> None:
        pass

    def get_trading_days(self, **_: object) -> list[str]:
        raise RuntimeError("Futu trading calendar unavailable")

    def close(self) -> None:
        pass
```

Configure the same deterministic clock and strategy snapshot as the skipped
test, then assert:

```python
assert cli.main([
    "trend-drawdown-preflight",
    "--config", str(tmp_path / "daily.env"),
    "--repo", str(tmp_path),
]) == 2
output = json.loads(capsys.readouterr().out)
assert output["status"] == "unavailable"
assert [item["status"] for item in output["markets"]] == [
    "unavailable", "unavailable", "unavailable"
]
assert all(
    "Futu trading calendar unavailable" in item["error"]
    for item in output["markets"]
)
```

- [ ] **Step 3: Run CLI tests and verify red**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_strategy_drawdown_cli.py::test_trend_drawdown_preflight_skips_missing_frozen_baseline_without_live_nav \
  tests/test_strategy_drawdown_cli.py::test_trend_drawdown_preflight_blocks_when_futu_calendar_is_unavailable \
  -q
```

Expected: the missing-baseline test fails with exit `2`; the external-failure
test locks down the existing `unavailable` behavior.

- [ ] **Step 4: Remove baseline policy from the CLI**

In the `trend-drawdown-preflight` branch:

- remove the one-time `strategy_drawdown_state_status` read;
- remove the `load_frozen_baseline` call and its early `ValueError`;
- construct successful market inputs with:

```python
inputs[market] = DrawdownMarketInput(
    market=market,
    strategy_snapshot=strategy,
    baseline_equity=None,
    source_date=source_date,
    entry_eligible_from=entry_eligible_from,
)
```

Keep the existing exception path for Futu/calendar/strategy-snapshot failures;
those inputs retain `error=str(exc)` and therefore remain `unavailable`.

Remove now-unused imports from `cli.py`. In the effective-strategy-date test,
remove the `strategy_drawdown_state_status` monkeypatch and assert captured
inputs use `baseline_equity is None`.

- [ ] **Step 5: Run focused CLI and preflight tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_drawdown_preflight.py \
  tests/test_strategy_drawdown_cli.py \
  -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit CLI delegation**

```bash
git add src/open_trader/cli.py tests/test_strategy_drawdown_cli.py
git commit -m "fix: let acceptance skip absent drawdown baselines"
```

### Task 4: Changelog, direct workflow, full verification, and acceptance

**Files:**

- Modify: `CHANGELOG.md`

**Interfaces:**

- Consumes: Tasks 1-3 completed and reviewed.
- Produces: a committed operator entry, a final accepted SHA, and a live review deployment serving that exact SHA.

- [ ] **Step 1: Add the dated operator-facing changelog entry**

Add under `## 2026-07-26` before the previous dated section:

```markdown
- Allowed Dashboard acceptance to skip only a genuinely absent completed-date
  frozen drawdown baseline while preserving visible market-level evidence;
  Futu/calendar outages still block, malformed baseline artifacts still fail,
  and runtime entry protection remains fail-closed.
```

Create the `## 2026-07-26` heading first if it does not yet exist.

- [ ] **Step 2: Run focused automated tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_drawdown_preflight.py \
  tests/test_strategy_drawdown_cli.py \
  -q
```

Expected: zero failures.

- [ ] **Step 3: Run the complete suite**

Run:

```bash
make test
```

Expected: zero failures.

- [ ] **Step 4: Commit the changelog before acceptance**

```bash
git add CHANGELOG.md
git commit -m "docs: record skippable drawdown acceptance"
git status --short --branch
```

Expected: clean worktree on `fix/acceptance-skip-missing-baseline`.

- [ ] **Step 5: Exercise both baseline paths without production mutation**

Use a temporary directory and call `run_drawdown_preflight` directly twice:

1. no report and `baseline_equity=None` must return overall `ready`, market
   `skipped`, reason `baseline_missing`, and no
   `data/trend_drawdown/state.json`;
2. a valid completed-date frozen report with a positive account net value and
   `drawdown_summary.state_status == "missing"` must return market
   `bootstrapped` and create state only inside the temporary directory.

The script must assert both outcomes and print:

```text
missing baseline: SKIPPED
valid baseline: BOOTSTRAPPED
```

- [ ] **Step 6: Run the affected real CLI workflow**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m open_trader trend-drawdown-preflight \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env \
  --repo /Users/ray/projects/open_trader/.worktrees/acceptance-skip-missing-baseline \
  --actor acceptance
```

Expected: JSON with top-level `ready`, or exit `2` only if a genuine
Futu/calendar dependency is unavailable. Record each market status. Do not
rewrite frozen reports or substitute live net value.

- [ ] **Step 7: Deploy the committed worktree for Dashboard checks**

Restart the port-8766 screen from this exact worktree:

```bash
screen -S open_trader_dashboard_8766 -X quit 2>/dev/null || true
screen -dmS open_trader_dashboard_8766 zsh -lc \
  'cd /Users/ray/projects/open_trader/.worktrees/acceptance-skip-missing-baseline && exec env PYTHONPATH=src .venv/bin/python -u -m open_trader dashboard --portfolio /Users/ray/projects/open_trader/data/latest/portfolio.csv --data-dir /Users/ray/projects/open_trader/data --reports-dir /Users/ray/projects/open_trader/reports --config /Users/ray/projects/open_trader/config/daily_premarket.env --poll-seconds 5 --host 127.0.0.1 --port 8766 >> /tmp/open_trader_dashboard_8766.log 2>&1'
```

Verify one listener on port 8766, the process PID and working directory, the
worktree HEAD SHA, a fresh `dashboard_runtime` log line, and HTTP `200`.

- [ ] **Step 8: Inspect background processes**

Run:

```bash
screen -ls
launchctl list | rg 'com\.open-trader\.(trend|premarket)' || true
ps aux | rg '[o]pen_trader (dashboard|trend-market|run-daily-premarket)'
```

Confirm only the Dashboard is changed by this branch. Do not restart trend
controllers.

- [ ] **Step 9: Run the final Dashboard gate**

Run exactly once after all prior steps:

```bash
make acceptance
```

Expected terminal result: `PASS`.

The gate may include visible `skipped` market rows when a completed-date
baseline is genuinely absent, but it must continue to the browser acceptance.
On `FAIL`, diagnose and fix, rerun focused/full checks, recommit, redeploy, and
rerun the gate. On `BLOCKED`, report the genuine external dependency blocker
and do not claim completion.

- [ ] **Step 10: Redeploy the exact accepted SHA**

Record:

```bash
git rev-parse HEAD
```

Restart `open_trader_dashboard_8766` with the Step 7 command without modifying
source or data. Verify the new PID, working directory, accepted SHA, fresh log
timestamp, and HTTP `200`.

- [ ] **Step 11: Provide the review URL**

Report `http://127.0.0.1:8766/`, the accepted SHA, PID, working directory,
focused/full test counts, direct missing/valid baseline results, real CLI market
statuses, and `make acceptance` `PASS`.

## Plan Self-Review

- Spec coverage: Task 1 distinguishes available, missing, and invalid completed-date data. Task 2 places lookup after recovery/predecessor checks and preserves state. Task 3 proves CLI success versus genuine external blocking. Task 4 covers changelog, direct missing/valid workflows, real CLI behavior, full tests, processes, final acceptance, and exact-SHA deployment.
- Scope: no strategy rule, live account substitution, controller relaxation, notification policy, dependency, or public workflow split is included.
- Type consistency: `load_frozen_baseline` returns `FrozenBaselineLookup`; `run_drawdown_preflight` retains its existing interface; CLI continues to map only `ready`, `failed`, and `unavailable` to exit codes `0`, `1`, and `2`.
