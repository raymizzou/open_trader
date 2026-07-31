# Dashboard Latest Trend Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make each Dashboard trend-report main view show the newest valid report immediately, including a next-execution-day US report generated while New York is still on the prior calendar date.

**Architecture:** Keep the existing strict artifact validator and projection path. Remove only the current-market-date cutoff from latest-report selection; invalid artifacts still fall through to the next valid artifact, and the existing history endpoint remains unchanged.

**Tech Stack:** Python 3.12, pytest, existing Open Trader Dashboard and acceptance workflow.

## Global Constraints

- No new dependency, report schema, state file, or controller-owned `latest` pointer.
- Do not change Dashboard layout, copy, interactions, report JSON, strategy actions, or historical artifacts.
- A report must still pass readability, schema, market, broker, chronology, frozen-fact, risk, and option-attention validation.
- Old reports remain available only through the existing history entry.
- Run `make acceptance` only once, as the final Dashboard gate after all source and changelog commits.
- After `PASS`, deploy the exact accepted SHA and provide a screenshot from that deployment.

---

### Task 1: Select the newest valid report without a calendar cutoff

**Files:**
- Modify: `tests/test_dashboard.py:3160`
- Modify: `src/open_trader/dashboard.py:949-990`
- Modify: `src/open_trader/dashboard.py:1969-1971`

**Interfaces:**
- Consumes: `_valid_trend_report_payload(payload, market, broker) -> tuple[date, date, date, datetime] | None`
- Produces: `_latest_valid_report_payload(reports_dir: Path, *, market: str, broker: str) -> tuple[Path, dict[str, Any], date, date, date, datetime] | None`
- Preserves: `_load_broker_trend_report(..., report_date: str, ...)`; `report_date` remains available to projection/status logic but no longer filters artifact selection.

- [ ] **Step 1: Replace the obsolete future-report expectation and add the US cross-timezone regression**

Rename `test_dashboard_trend_report_skips_future_candidate` to
`test_dashboard_trend_report_selects_latest_valid_next_execution_day`.
Write each payload with `metadata.run_date` equal to its execution date and
assert that a Dashboard date of `2026-07-15` selects `2026-07-16.json`:

```python
def test_dashboard_trend_report_selects_latest_valid_next_execution_day(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    reports_dir = config.reports_dir / "trend_us_tiger"
    reports_dir.mkdir(parents=True)
    base = {
        "account": serialized_trend_account(fresh=True),
        "strategy_judgments": {
            "formal_actions": [],
            "holding_decisions": [],
            "top10_candidates": [],
        },
        "option_attention": [],
    }
    for execution_date, as_of_date in (
        ("2026-07-15", "2026-07-14"),
        ("2026-07-16", "2026-07-15"),
    ):
        (reports_dir / f"{execution_date}.json").write_text(json.dumps({
            **base,
            "execution_date": execution_date,
            "as_of_date": as_of_date,
            "generated_at": f"{execution_date}T07:30:00+08:00",
            "metadata": {
                "market": "US",
                "broker": "tiger",
                "run_date": execution_date,
            },
        }), encoding="utf-8")

    report = dashboard_module._load_trend_reports(
        config.data_dir,
        config.reports_dir,
        today=date(2026, 7, 15),
    )["tiger"]

    assert report["available"] is True
    assert report["artifact"] == "2026-07-16.json"
    assert report["report_date"] == "2026-07-16"
```

Add a direct cross-timezone test using `now=2026-07-16T07:30:00+08:00`,
which is still `2026-07-15` in New York:

```python
def test_dashboard_us_main_view_uses_latest_report_before_new_york_midnight(
    tmp_path: Path,
) -> None:
    config = dashboard_config(tmp_path)
    reports_dir = config.reports_dir / "trend_us_tiger"
    reports_dir.mkdir(parents=True)
    base = {
        "account": serialized_trend_account(fresh=True),
        "strategy_judgments": {
            "formal_actions": [],
            "holding_decisions": [],
            "top10_candidates": [],
        },
        "option_attention": [],
    }
    for execution_date, as_of_date, generated_at in (
        ("2026-07-15", "2026-07-14", "2026-07-15T23:57:00+08:00"),
        ("2026-07-16", "2026-07-15", "2026-07-16T07:30:00+08:00"),
    ):
        (reports_dir / f"{execution_date}.json").write_text(json.dumps({
            **base,
            "execution_date": execution_date,
            "as_of_date": as_of_date,
            "generated_at": generated_at,
            "metadata": {
                "market": "US",
                "broker": "tiger",
                "run_date": execution_date,
            },
        }), encoding="utf-8")

    report = dashboard_module._load_trend_reports(
        config.data_dir,
        config.reports_dir,
        now=datetime(
            2026, 7, 16, 7, 30, tzinfo=dashboard_module.SHANGHAI
        ),
    )["tiger"]

    assert dashboard_module._trend_market_date(
        "US",
        now=datetime(
            2026, 7, 16, 7, 30, tzinfo=dashboard_module.SHANGHAI
        ),
    ) == date(2026, 7, 15)
    assert report["artifact"] == "2026-07-16.json"
```

- [ ] **Step 2: Run the two tests and verify RED**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/pytest -q \
  tests/test_dashboard.py::test_dashboard_trend_report_selects_latest_valid_next_execution_day \
  tests/test_dashboard.py::test_dashboard_us_main_view_uses_latest_report_before_new_york_midnight
```

Expected: both fail because the selector returns the valid `2026-07-15`
artifact instead of `2026-07-16.json`.

- [ ] **Step 3: Remove only the date cutoff from the selector**

Change the selector signature and delete the `today` calculation and two
future-date rejection branches:

```python
def _latest_valid_report_payload(
    reports_dir: Path, *, market: str, broker: str
) -> tuple[Path, dict[str, Any], date, date, date, datetime] | None:
    matches: list[
        tuple[date, datetime, date, str, Path, dict[str, Any], date]
    ] = []
    for path in reports_dir.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        chronology = _valid_trend_report_payload(
            payload, market=market, broker=broker
        )
        if chronology is None:
            continue
        execution_date, as_of_date, freshness_date, generated_at = chronology
        matches.append(
            (
                freshness_date,
                generated_at,
                execution_date,
                path.name,
                path,
                payload,
                as_of_date,
            )
        )
    if not matches:
        return None
    freshness_date, generated_at, execution_date, _, path, payload, as_of_date = max(
        matches, key=lambda item: item[:4]
    )
    return path, payload, execution_date, as_of_date, freshness_date, generated_at
```

Update the sole caller:

```python
selected = _latest_valid_report_payload(
    reports_dir, market=market, broker=broker
)
```

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/pytest -q \
  tests/test_dashboard.py::test_dashboard_trend_report_selects_latest_valid_next_execution_day \
  tests/test_dashboard.py::test_dashboard_us_main_view_uses_latest_report_before_new_york_midnight \
  tests/test_dashboard.py::test_dashboard_trend_report_skips_invalid_newest_candidate \
  tests/test_dashboard.py::test_dashboard_trend_report_ranks_revisions_by_generated_instant \
  tests/test_dashboard.py::test_trend_report_history_uses_payload_date_and_keeps_revisions
```

Expected: `5 passed`.

- [ ] **Step 5: Run the complete Dashboard unit module**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/pytest -q tests/test_dashboard.py
```

Expected: all tests pass; baseline before implementation was
`245 passed in 0.65s`.

- [ ] **Step 6: Review and commit the behavior change**

Use the `code-review` skill against `b7694f2`, resolve every correctness or
spec finding, then run:

```bash
git diff --check
git add src/open_trader/dashboard.py tests/test_dashboard.py
git commit -m "fix: show latest valid trend report"
```

---

### Task 2: Changelog, final acceptance, merge, deploy, and UI proof

**Files:**
- Modify: `CHANGELOG.md`
- Runtime output only: Dashboard screenshot from the exact deployed SHA

**Interfaces:**
- Consumes: the committed selector behavior from Task 1.
- Produces: an operator-facing changelog entry, final accepted SHA, deployed live Dashboard, and screenshot evidence.

- [ ] **Step 1: Add the required changelog entry before merge**

Under `## 2026-07-31`, add:

```markdown
- Made each Dashboard trend-report main view select the newest valid artifact
  immediately, including the next US execution-day report before New York
  midnight. Invalid artifacts still fall through safely, and all older valid
  reports remain available from history.
```

Run:

```bash
git diff --check
git add CHANGELOG.md
git commit -m "docs: log latest trend report selection"
```

- [ ] **Step 2: Run the full automated test suite**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/pytest -q
```

Expected: all tests pass with no failures.

- [ ] **Step 3: Run the final Dashboard acceptance gate**

Ensure the exact worktree has the ignored runtime config/data links required by
the existing acceptance workflow, without changing tracked source. Then run
only once as the final gate:

```bash
make acceptance
```

Expected: `PASS`. Do not merge, deploy, or report completion on `FAIL` or
`BLOCKED`.

- [ ] **Step 4: Merge the exact accepted branch into local `main`**

Record the accepted SHA, confirm the worktree is clean, and merge without
touching unrelated dirty files in the root checkout. Confirm:

```bash
git merge --ff-only fix/dashboard-latest-trend-report
git rev-parse main
git rev-parse fix/dashboard-latest-trend-report
git status --short
```

If `main` advanced, merge latest local `main` into the feature branch first,
rerun affected tests and `make acceptance`, then merge the exact newly accepted
SHA.

- [ ] **Step 5: Push `main` and redeploy the exact accepted SHA**

Push `main`, redeploy Dashboard and any launchd services whose source SHA is
expected to match `main`, then verify:

```text
origin/main SHA == accepted SHA
Dashboard PID is new
Dashboard cwd points at the accepted worktree/source
Dashboard runtime Git SHA == accepted SHA
fresh logs contain the new PID/start timestamp and no new errors
http://127.0.0.1:8766/ returns HTTP 200
```

This post-acceptance restart does not require another acceptance run when it
uses the exact accepted SHA and no source or data changed.

- [ ] **Step 6: Verify the live US report and send the screenshot**

Check `http://127.0.0.1:8766/api/dashboard` and confirm:

```text
trend_reports.tiger.artifact == "2026-07-30.json"
trend_reports.tiger.industry_context_status.current_complete == true
trend_reports.tiger.industry_contexts has 2 entries
```

Open the deployed Dashboard, select 老虎 → 趋势报告, and capture the visible
industry-context section showing the latest report's populated contexts.
Include that screenshot inline in the handoff together with the URL, accepted
SHA, test count, PID, cwd, runtime SHA, fresh-log result, and HTTP 200 result.
