# Dashboard Acceptance Single-Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the fixed 125-second wait while preserving one real quote/account refresh and a before/after Dashboard stability check.

**Architecture:** Keep the existing acceptance runner and API contracts. Capture the Dashboard before refresh, call `/api/quotes` once, capture the Dashboard after refresh, then reuse all existing payload, process, log, screenshot, and browser checks.

**Tech Stack:** Python 3.12, pytest, Make, Playwright with system Chrome.

## Global Constraints

- Do not add dependencies or new services.
- Remove `WAIT_SECONDS` and `--wait-seconds`; do not leave dead compatibility configuration.
- Keep `PASS`, `FAIL`, and `BLOCKED` semantics unchanged.
- Run `make acceptance` only as the final gate, then restart the exact accepted SHA and verify PID, cwd, SHA, fresh logs, and HTTP 200.
- Preserve unrelated worktree changes.

---

### Task 1: Replace Two Timed Quote Refreshes With One Real Refresh

**Files:**
- Modify: `tests/test_dashboard_acceptance.py:1-320,740-790,2760-2770`
- Modify: `src/open_trader/dashboard_acceptance.py:1-15,278-290,1480-1559`
- Modify: `Makefile:9-14`
- Modify: `AGENTS.md:31-32`
- Modify: `README.zh-CN.md:423-424`

**Interfaces:**
- Consumes: existing `_fetch_payload(url) -> dict`, `_fetch_quotes_payload(url) -> dict`, `validate_dashboard_payload(...) -> list[str]`, and `validate_quotes_payload(payload) -> list[str]`.
- Produces: `main(argv) -> int` with one `_fetch_quotes_payload` call between two `_fetch_payload` calls and no configurable sleep.

- [ ] **Step 1: Write the failing acceptance-runner test**

Change `_run_acceptance_main_with_reports` so only one quote payload exists, any sleep fails the test, and no wait option is passed:

```python
payloads = iter({"reports_dir": str(path)} for path in report_dirs)
quote_payloads = iter((valid_quotes_payload(),))

monkeypatch.setattr(
    dashboard_acceptance.time,
    "sleep",
    lambda seconds: pytest.fail(f"acceptance slept for {seconds} seconds"),
)
monkeypatch.setattr(
    dashboard_acceptance, "_fetch_quotes_payload", lambda url: next(quote_payloads)
)

status = dashboard_acceptance.main([
    "--expected-root", str(worktree),
    "--log", str(log_path),
])
```

Extend the Makefile contract test:

```python
assert "WAIT_SECONDS" not in makefile
assert "--wait-seconds" not in makefile
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_dashboard_acceptance.py::test_acceptance_main_passes_external_api_reports_dir_to_browser_check \
  tests/test_dashboard_acceptance.py::test_make_acceptance_allows_an_isolated_dashboard_url_and_log
```

Expected: FAIL because the current runner sleeps and the Makefile still contains `WAIT_SECONDS` and `--wait-seconds`.

- [ ] **Step 3: Implement the single-refresh flow**

Delete `validate_quote_refresh_cycle`, its `datetime` import, the parser's `--wait-seconds` argument, and the second quote fetch. Keep two Dashboard snapshots around the one quote request:

```python
first = _fetch_payload(args.url)
first_reports_dir = _effective_reports_dir(first, process_cwd=cwd)
errors.extend(validate_dashboard_payload(
    first, expected_cn=args.expected_cn,
    expected_eastmoney_cny=args.expected_eastmoney_cny,
    expected_rows=args.expected_rows,
    expected_phillips_total=phillips_total,
    expected_phillips_period=phillips_period,
))

quotes = _fetch_quotes_payload(args.url)
errors.extend(validate_quotes_payload(quotes))

second = _fetch_payload(args.url)
browser_payload = second
reports_dir = _effective_reports_dir(second, process_cwd=cwd)
```

Use refresh-oriented errors:

```python
if first_reports_dir != reports_dir:
    errors.append("账户刷新前后的 Dashboard reports_dir 不一致")
if dashboard_signature(first) != dashboard_signature(second):
    errors.append("账户刷新后的 Dashboard 数据不稳定")
```

Remove the final Makefile argument:

```make
acceptance: test
	PYTHONPATH=src .venv/bin/python -m open_trader.dashboard_acceptance \
		--url "$(DASHBOARD_URL)" \
		--log "$(DASHBOARD_LOG)" \
		--expected-root "$(CURDIR)"
```

- [ ] **Step 4: Remove obsolete refresh-cycle tests and update policy text**

Remove the `validate_quote_refresh_cycle` import and its timestamp parameterized test. Assert the parser no longer exposes the setting:

```python
assert args.expected_eastmoney_cny is None
assert not hasattr(args, "wait_seconds")
```

Change the reports-directory assertion to:

```python
assert "账户刷新前后的 Dashboard reports_dir 不一致" in result["errors"]
```

In `AGENTS.md`, replace `two refresh cycles` with `one live account/quote refresh`. In `README.zh-CN.md`, replace `两个后台刷新周期` with `一次真实账户与行情刷新`.

- [ ] **Step 5: Run focused verification and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_dashboard_acceptance.py
```

Expected: all tests in `tests/test_dashboard_acceptance.py` pass with no sleep.

- [ ] **Step 6: Inspect and commit only the acceptance change**

Run:

```bash
git diff --check
git diff -- Makefile AGENTS.md README.zh-CN.md \
  src/open_trader/dashboard_acceptance.py tests/test_dashboard_acceptance.py
git add Makefile AGENTS.md README.zh-CN.md \
  src/open_trader/dashboard_acceptance.py tests/test_dashboard_acceptance.py
git commit -m "test: shorten dashboard acceptance refresh"
```

Expected: the commit contains only the five listed files.

- [ ] **Step 7: Run the exact-SHA final acceptance gate**

Create a detached validation worktree from the committed SHA so unrelated dirty files cannot enter the running process:

```bash
git worktree add --detach /tmp/open_trader-acceptance-single-refresh HEAD
ln -s /Users/ray/projects/open_trader/.venv \
  /tmp/open_trader-acceptance-single-refresh/.venv
screen -S open_trader_acceptance_single_refresh -X quit 2>/dev/null || true
screen -dmS open_trader_acceptance_single_refresh zsh -lc \
  'cd /tmp/open_trader-acceptance-single-refresh && export PYTHONPATH=src && exec .venv/bin/python -m open_trader dashboard --portfolio /Users/ray/projects/open_trader/data/latest/portfolio.csv --data-dir /Users/ray/projects/open_trader/data --reports-dir /Users/ray/projects/open_trader/reports --poll-seconds 5 --host 127.0.0.1 --port 18766 >>/tmp/open_trader_dashboard_18766.log 2>&1'
```

After confirming HTTP readiness, run only the final gate:

```bash
cd /tmp/open_trader-acceptance-single-refresh
DASHBOARD_URL=http://127.0.0.1:18766 \
DASHBOARD_LOG=/tmp/open_trader_dashboard_18766.log \
make acceptance
```

Expected: all tests pass and the final JSON reports `"status": "PASS"`, no errors, and no blocker. On `FAIL`, fix and repeat; on `BLOCKED`, report the blocker.

- [ ] **Step 8: Redeploy and verify the exact accepted SHA**

Restart the same detached checkout without source or data changes, then verify the new process:

```bash
screen -S open_trader_acceptance_single_refresh -X quit
: > /tmp/open_trader_dashboard_18766.log
screen -dmS open_trader_acceptance_single_refresh zsh -lc \
  'cd /tmp/open_trader-acceptance-single-refresh && export PYTHONPATH=src && exec .venv/bin/python -m open_trader dashboard --portfolio /Users/ray/projects/open_trader/data/latest/portfolio.csv --data-dir /Users/ray/projects/open_trader/data --reports-dir /Users/ray/projects/open_trader/reports --poll-seconds 5 --host 127.0.0.1 --port 18766 >>/tmp/open_trader_dashboard_18766.log 2>&1'
lsof -nP -iTCP:18766 -sTCP:LISTEN
git -C /tmp/open_trader-acceptance-single-refresh rev-parse HEAD
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18766/
tail -20 /tmp/open_trader_dashboard_18766.log
```

Expected: a new PID, cwd `/tmp/open_trader-acceptance-single-refresh`, the accepted SHA, fresh startup logs without error markers, and HTTP `200`.
