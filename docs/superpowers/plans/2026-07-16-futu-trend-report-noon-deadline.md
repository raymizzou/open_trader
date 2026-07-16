# Futu Trend Report Noon Deadline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep the Futu US trend report retrying until 12:00 Asia/Shanghai, recover the prematurely failed 2026-07-16 batch, and deploy only after full Dashboard acceptance passes.

**Architecture:** Reuse the existing `MARKET_SETTINGS` deadline and `_run_market_trend_retry` loop; only the US deadline constant changes. Preserve the existing ten-minute polling, fail-closed report semantics, and daily Feishu delivery ledger. Recover today's batch by archiving its unsent premature failure ledger before running the normal report command.

**Tech Stack:** Python 3.12, pytest, launchd, existing Open Trader CLI, Playwright-backed Dashboard acceptance.

## Global Constraints

- US report start remains 09:00 Asia/Shanghai and polling remains every 600 seconds.
- US report deadline becomes exactly 12:00 Asia/Shanghai.
- HK and CN schedules remain unchanged.
- Never reuse a stale report when fresh trend data is unavailable.
- Archive, do not delete, the unsent 2026-07-16 failure ledger.
- Add no dependencies or new scheduler abstractions.
- `make acceptance` must return `PASS` before completion.

---

### Task 1: Extend the US Retry Window

**Files:**
- Modify: `tests/test_market_trend.py:232-280`
- Modify: `src/open_trader/market_trend.py:58-61`
- Modify: `src/open_trader/market_trend.py:788-793`

**Interfaces:**
- Consumes: `run_market_trend_report(..., now_fn, sleep_fn, attempt_fn) -> AShareTrendRunResult`
- Produces: `MARKET_SETTINGS["US"]["deadline"] == time(12)` while preserving the existing retry function signature.

- [ ] **Step 1: Add a failing regression test for retrying past 10:00**

Add this test after `test_market_report_retries_every_ten_minutes_and_stops_after_success`:

```python
def test_market_report_keeps_retrying_after_old_ten_deadline(
    tmp_path: Path,
) -> None:
    attempts = iter([
        AShareTrendRunResult("waiting", None, None),
        AShareTrendRunResult("waiting", None, None),
        AShareTrendRunResult("generated", Path("report.md"), Path("report.json")),
    ])
    times = iter([
        datetime(2026, 7, 15, 10, 0, tzinfo=SHANGHAI),
        datetime(2026, 7, 15, 11, 40, tzinfo=SHANGHAI),
    ])
    sleeps: list[float] = []

    result = run_market_trend_report(
        config=config(tmp_path),
        market="US",
        run_date="2026-07-15",
        notifier=NullNotifier(),
        attempt_fn=lambda **kwargs: next(attempts),
        now_fn=lambda: next(times),
        sleep_fn=sleeps.append,
    )

    assert result.status == "generated"
    assert sleeps == [600.0, 600.0]
```

- [ ] **Step 2: Run the regression test and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_market_trend.py::test_market_report_keeps_retrying_after_old_ten_deadline -q
```

Expected: FAIL because the existing 10:00 deadline returns `failed` on the first waiting attempt.

- [ ] **Step 3: Move the existing deadline failure test to noon**

In `test_market_report_failure_owns_day_at_one_hour_deadline`, rename the test and change its pinned time:

```python
def test_market_report_failure_owns_day_at_noon_deadline(tmp_path: Path) -> None:
    now = datetime(2026, 7, 15, 12, 0, tzinfo=SHANGHAI)
```

Keep the assertions proving the Feishu title, failure message, and ledger status are frozen once at the deadline.

- [ ] **Step 4: Implement the minimal deadline change**

Change only the US deadline and correct the now-inaccurate local failure text:

```python
MARKET_SETTINGS = {
    "US": {"broker": "futu", "currency": "USD", "asset": "美股", "deadline": time(12)},
    "HK": {"broker": "phillips", "currency": "HKD", "asset": "港股", "deadline": time(19)},
}
```

```python
f"{last_error}；本轮重试窗口已结束。"
```

- [ ] **Step 5: Run focused tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_market_trend.py tests/test_premarket_cli.py -q
```

Expected: all tests PASS, including retry at 10:00, success after 11:40, and failure at 12:00.

- [ ] **Step 6: Commit the behavior change**

```bash
git add src/open_trader/market_trend.py tests/test_market_trend.py
git commit -m "fix: extend US trend report deadline"
```

---

### Task 2: Recover the 2026-07-16 Futu Report

**Files:**
- Archive: `data/trend_us_futu/daily_delivery/2026-07-16.pre-noon-fix-20260716T100000+0800.json`
- Generate: `reports/trend_us_futu/2026-07-15.json`
- Generate: `reports/trend_us_futu/2026-07-15.md`
- Update through existing workflow: `data/trend_us_futu/protection_state.json`

**Interfaces:**
- Consumes: `python -m open_trader trend-market-report --market US --date 2026-07-16`
- Produces: an available Futu report with `execution_date=2026-07-16` and `as_of_date=2026-07-15`.

- [ ] **Step 1: Verify the old ledger is the approved recovery case**

Run:

```bash
.venv/bin/python - <<'PY'
import json
from pathlib import Path
p = Path("data/trend_us_futu/daily_delivery/2026-07-16.json")
d = json.loads(p.read_text())
assert d["status"] == "delivery_failed"
assert d["title"] == "【富途｜美股趋势报告生成失败｜2026-07-16】"
print(d["status"], d["title"])
PY
```

Expected: `delivery_failed` and the exact Futu failure title.

- [ ] **Step 2: Stop the live US protection watcher if it is running**

Inspect `launchctl print gui/$(id -u)/com.open-trader.trend-us-watch` and its PID. Stop only that watcher before report generation so it cannot retain or race pre-change protection state.

- [ ] **Step 3: Archive the unsent premature failure ledger**

Use a timestamped destination in the same directory:

```bash
archive="data/trend_us_futu/daily_delivery/2026-07-16.pre-noon-fix-20260716T100000+0800.json"
mv data/trend_us_futu/daily_delivery/2026-07-16.json "$archive"
test -f "$archive"
```

Verify the archive exists and the canonical ledger path no longer exists before running the report.

- [ ] **Step 4: Run the real Futu report workflow**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m open_trader trend-market-report \
  --market US --date 2026-07-16 --config config/daily_premarket.env
```

Expected: status `generated`, with report JSON and Markdown paths under `reports/trend_us_futu/`.

- [ ] **Step 5: Validate the report and delivery ledger**

Run a Python assertion that checks:

```python
assert report["execution_date"] == "2026-07-16"
assert report["as_of_date"] == "2026-07-15"
assert report["metadata"]["broker"] == "futu"
assert report["metadata"]["market"] == "US"
assert report["process_version"] == subprocess.check_output(
    ["git", "rev-parse", "HEAD"], text=True
).strip()
assert ledger["status"] == "sent"
```

Also verify fresh `generated` entries in `data/trend_us_futu/run.log` and no traceback in the launchd stderr log.

- [ ] **Step 6: Restart and verify the US watcher**

Start the same launchd watcher again. Verify its new PID, working directory, current Git SHA, and fresh log timestamp.

---

### Task 3: Deploy and Accept the Exact SHA

**Files:**
- No source changes.
- Runtime log: `/tmp/open_trader_dashboard_<accepted-sha>.log`

**Interfaces:**
- Consumes: committed Git SHA from Task 1 and production `reports/`.
- Produces: review Dashboard at `http://127.0.0.1:8766/` serving the exact accepted SHA.

- [ ] **Step 1: Restart Dashboard on the committed SHA using production reports**

Stop the current 8766 Dashboard and start:

```bash
PYTHONPATH=src .venv/bin/python -u -m open_trader dashboard \
  --portfolio data/latest/portfolio.csv --data-dir data --reports-dir reports \
  --poll-seconds 5 --host 127.0.0.1 --port 8766
```

Verify `/api/dashboard` reports `trend_reports.futu.available == true`.

- [ ] **Step 2: Run the full acceptance gate**

Run:

```bash
DASHBOARD_LOG=/tmp/open_trader_dashboard_<accepted-sha>.log make acceptance
```

Expected: all automated tests pass and the final JSON status is `PASS` with no errors or blocker.

- [ ] **Step 3: Redeploy the exact accepted SHA**

After `PASS`, restart the Dashboard once more from the unchanged accepted SHA using `reports/`. Do not regenerate reports or modify source after acceptance.

- [ ] **Step 4: Verify live review state**

Verify the new Dashboard PID, process start timestamp, working directory, Git SHA, clean fresh log, HTTP 200 for `/` and `/api/dashboard`, and a real Chrome flow that opens the Futu report and returns to holdings.

- [ ] **Step 5: Report the review URL**

Provide `http://127.0.0.1:8766/` only after all checks above pass.
