# Safe Latest Trend Revision Display Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan.

**Goal:** Show the newest validated trend-report revision on the current Dashboard when its complete formal action list is unchanged, while keeping all execution state bound to the immutable execution batch.

**Architecture:** Keep report selection and execution identity as two explicit values inside `_project_broker_trend_report`. The selected report controls displayed report content; the execution batch SHA controls action-event lookup. Compare the newest and locked payloads' complete `strategy_judgments.formal_actions` lists before allowing the newest payload to remain selected.

**Tech Stack:** Python 3, pytest, JSON report artifacts, the existing Dashboard server and Playwright acceptance runner.

---

### Task 1: Prove both report-selection branches

**Files:**
- Modify: `tests/test_dashboard_web.py:264-370`

**Step 1: Write the failing display-only revision test**

Add `test_dashboard_displays_latest_revision_when_formal_actions_are_unchanged`.
Build a base report and an `-r1` report with identical `formal_actions`, then add
this corrected holding decision only to the revision:

```python
revised["strategy_judgments"]["holding_decisions"] = [{
    "symbol": "VIXY",
    "name": "ProShares VIX",
    "action": "HOLD",
    "reason": "trend_intact",
    "strength": "96.1",
    "active_protection_line": "8.42",
}]
```

Lock the batch to the base report and write a BUY execution event whose
`report_sha256` is the base hash. Assert:

```python
assert report["artifact"] == "2026-07-17-r1.json"
assert report["report_sha256"] == _report_hash(revised)
assert report["execution_batch"]["report_sha256"] == _report_hash(base)
assert report["latest_report_sha256"] == _report_hash(revised)
assert report["revision_anomaly"] is True
assert report["buy_actions"][0]["execution"]["status"] == "missed"
assert report["counts"]["hold"] == 1
assert report["counts"]["review"] == 0
assert report["hold_actions"][0]["active_protection_line"] == "8.42"
```

The existing
`test_dashboard_projects_locked_batch_when_latest_report_is_a_revision`
already proves the opposite branch by changing the revision's formal-action
symbol. Keep its locked-artifact assertions unchanged.

**Step 2: Run the focused tests to verify RED**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_dashboard_web.py::test_dashboard_displays_latest_revision_when_formal_actions_are_unchanged \
  tests/test_dashboard_web.py::test_dashboard_projects_locked_batch_when_latest_report_is_a_revision
```

Expected: the new test fails because the Dashboard still selects
`2026-07-17.json`; the changed-action regression test passes.

### Task 2: Separate display selection from execution identity

**Files:**
- Modify: `src/open_trader/dashboard.py:1776-1892`

**Step 1: Retain the newest payload only when formal actions match**

Inside the valid execution-batch branch, assemble the locked report tuple but
replace `selected` only when the complete formal-action lists differ:

```python
locked_selected = (
    path,
    payload,
    execution_date,
    as_of_date,
    freshness_date,
    generated_at,
)
if (
    latest_payload["strategy_judgments"]["formal_actions"]
    != payload["strategy_judgments"]["formal_actions"]
):
    selected = locked_selected
```

Continue exposing the validated batch and calculating `revision_anomaly` from
the locked and latest hashes.

**Step 2: Load execution events with the locked batch SHA**

After calculating the selected report hash, derive a distinct execution SHA:

```python
execution_report_sha256 = (
    str(execution_batch["report_sha256"])
    if execution_batch is not None
    else report_sha256
)
```

Pass `execution_report_sha256` to `_trend_action_executions`. Do not mutate the
batch or either report artifact.

**Step 3: Run the focused tests to verify GREEN**

Run the two tests from Task 1.

Expected: `2 passed`.

**Step 4: Run the surrounding regression tests**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_dashboard_web.py -k 'execution_batch or revision' \
  tests/test_dashboard.py::test_trend_report_history_uses_payload_date_and_keeps_revisions
```

Expected: all selected tests pass, including invalid-batch blocking and history.

### Task 3: Record the operator-facing behavior and commit

**Files:**
- Modify: `CHANGELOG.md:7-15`

**Step 1: Add the dated changelog entry**

Under `2026-07-27`, add a concise entry explaining that the Dashboard may show a
newer same-action trend-report revision while execution remains locked to the
original batch, and list the focused verification.

**Step 2: Check the diff**

Run:

```bash
git diff --check
git diff -- src/open_trader/dashboard.py tests/test_dashboard_web.py CHANGELOG.md
```

Expected: no whitespace errors; the diff contains only the selection fix,
regression test, and changelog entry.

**Step 3: Commit the behavior change**

Run:

```bash
git add src/open_trader/dashboard.py tests/test_dashboard_web.py CHANGELOG.md
git commit -m "fix: show safe trend report revisions"
```

### Task 4: Deploy the candidate and run the final acceptance gate

**Files:**
- Runtime: `/tmp/open_trader_dashboard_8766.log`
- Runtime: `screen` session `open_trader_dashboard_8766`

**Step 1: Provide the worktree virtual environment**

Run:

```bash
ln -s /Users/ray/projects/open_trader/.venv .venv
```

Expected: `.venv/bin/python` resolves to the repository virtual environment.

**Step 2: Start the candidate SHA on the review port**

Stop only the named Dashboard session, verify that no old listener remains on
port 8766, then start:

```bash
screen -dmS open_trader_dashboard_8766 zsh -lc \
  'cd /Users/ray/projects/open_trader/.worktrees/safe-latest-trend-revision-display && exec env PYTHONPATH=src .venv/bin/python -u -m open_trader dashboard --portfolio /Users/ray/projects/open_trader/data/latest/portfolio.csv --data-dir /Users/ray/projects/open_trader/data --reports-dir /Users/ray/projects/open_trader/reports --config /Users/ray/projects/open_trader/config/daily_premarket.env --poll-seconds 5 --host 127.0.0.1 --port 8766 >> /tmp/open_trader_dashboard_8766.log 2>&1'
```

Verify the PID, working directory, Git SHA, fresh log timestamp, and HTTP 200
before the acceptance run.

**Step 3: Run the final gate once**

Run:

```bash
make acceptance
```

Expected final status: `PASS`. A `FAIL` must be fixed and rerun; a `BLOCKED`
must be reported as blocked.

### Task 5: Redeploy the exact accepted SHA and verify the user's actual page

**Files:**
- Runtime: `/tmp/open_trader_dashboard_8766.log`
- URL: `http://127.0.0.1:8766`

**Step 1: Record the accepted SHA and restart without source/data changes**

Record `git rev-parse HEAD`, restart the named Dashboard session with the same
Task 4 command, and confirm the restarted process still resolves to that exact
SHA and worktree.

**Step 2: Verify process, logs, and HTTP**

Confirm:

- a new Dashboard PID;
- working directory is the isolated worktree;
- Git SHA equals the accepted SHA;
- fresh logs contain no traceback or error;
- `http://127.0.0.1:8766` returns HTTP 200.

**Step 3: Verify the default current report in a real browser**

Open the default Phillips Trend Report—not report history—and assert it visibly
contains:

```text
持有 1
复核 0
00939 建设银行
继续持有
96.1
8.42
```

Also assert the existing revision warning remains visible and says execution is
locked to the original batch. Check the browser console for errors. Leave this
default current report open for review.

**Step 4: Report only evidence-backed completion**

Provide the acceptance result, accepted SHA, new PID/worktree, log/HTTP/browser
evidence, and the review URL. Do not claim completion unless every assertion in
this task passed.
