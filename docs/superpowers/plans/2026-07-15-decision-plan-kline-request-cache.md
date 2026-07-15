# Decision Plan K-line Request Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reuse Futu history returned for a requested range even when the latest returned bar precedes the requested calendar end date.

**Architecture:** Keep the existing `_RangeCachingProvider` seam and change only how successful request coverage is recorded. Cache requested bounds for the lifetime of one decision-plan batch; continue filtering cached bars for narrower callers and continue forwarding empty, failed, or out-of-range requests.

**Tech Stack:** Python 3.12, pytest, existing Futu quote adapter, Make.

## Global Constraints

- Do not change failure notification counters.
- Do not add retries, rate limiting, dependencies, or new abstractions.
- Do not cache empty responses or exceptions.
- Run the real US dry-run and inspect fresh OpenD requests before acceptance.
- Run `make acceptance` after the modification; only `PASS` is completion.

---

### Task 1: Reuse Requested K-line Coverage

**Files:**
- Modify: `src/open_trader/decision_plan_generation.py:30-43`
- Test: `tests/test_decision_plan_generation.py`

**Interfaces:**
- Consumes: `DailyKlineProvider.get_daily_kline(futu_symbol: str, *, start: str, end: str) -> list[DailyKlineBar]`
- Produces: unchanged `_RangeCachingProvider.get_daily_kline(futu_symbol: str, *, start: str, end: str) -> list[object]`

- [ ] **Step 1: Write the failing regression test**

Add `_RangeCachingProvider` to the existing import and add this test:

```python
from open_trader.decision_plan_generation import (
    _RangeCachingProvider,
    generate_daily_decision_plans,
)


def test_range_cache_reuses_requested_end_after_latest_returned_bar() -> None:
    raw_provider = PriceProvider()
    provider = _RangeCachingProvider(raw_provider)

    provider.get_daily_kline(
        "US.MSFT",
        start="2025-04-20",
        end="2026-07-15",
    )
    bars = provider.get_daily_kline(
        "US.MSFT",
        start="2026-07-01",
        end="2026-07-15",
    )

    assert raw_provider.calls == 1
    assert bars
    assert all("2026-07-01" <= bar.date <= "2026-07-15" for bar in bars)
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_decision_plan_generation.py::test_range_cache_reuses_requested_end_after_latest_returned_bar -q
```

Expected: `FAIL` because `raw_provider.calls` is `2`, proving that the current cache misses when returned history ends before `2026-07-15`.

- [ ] **Step 3: Implement the minimal cache fix**

In `_RangeCachingProvider.get_daily_kline`, replace returned-date coverage with requested coverage:

```python
bars = list(self.provider.get_daily_kline(futu_symbol, start=start, end=end))
if bars:
    self._ranges.setdefault(futu_symbol, []).append((start, end, bars))
return bars
```

- [ ] **Step 4: Run the focused and relevant tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_decision_plan_generation.py::test_range_cache_reuses_requested_end_after_latest_returned_bar -q
.venv/bin/python -m pytest tests/test_decision_plan_generation.py tests/test_futu_quote.py tests/test_backtest_prices.py tests/test_strategy_backtest.py -q
```

Expected: the focused test reports `1 passed`; all relevant test files pass with no failures.

- [ ] **Step 5: Review the exact diff and commit the implementation**

Run:

```bash
git diff --check
git diff -- src/open_trader/decision_plan_generation.py tests/test_decision_plan_generation.py
git add src/open_trader/decision_plan_generation.py tests/test_decision_plan_generation.py docs/superpowers/plans/2026-07-15-decision-plan-kline-request-cache.md
git commit -m "fix: reuse decision plan kline requests"
```

Expected: one production-line behavior change, one regression test, this plan, and no unrelated files in the commit.

- [ ] **Step 6: Run the full automated suite**

Run:

```bash
make test
```

Expected: pytest exits `0` with no failed tests.

- [ ] **Step 7: Run the affected real workflow without notifications**

Confirm the scheduled job is not already running, then execute:

```bash
launchctl print gui/$(id -u)/com.open-trader.premarket.us | sed -n '1,80p'
PYTHONPATH=src .venv/bin/python -m open_trader run-daily-premarket \
  --market US \
  --date 2026-07-15 \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env \
  --dry-run
```

Expected: launchd reports `state = not running`; the dry-run exits `0` and writes a non-failed US status without sending notifications.

- [ ] **Step 8: Inspect fresh process and OpenD evidence**

Run:

```bash
launchctl print gui/$(id -u)/com.open-trader.premarket.us | sed -n '1,100p'
pgrep -afil 'run-daily-premarket|daily_premarket' || true
rg --text -n '获取历史K线频率太高|每30秒最多60次' \
  /Users/ray/.com.futunn.FutuOpenD/Log/GTWLog_* | tail -20
jq '{started_at,finished_at,status,readiness,error}' \
  data/runs/2026-07-15/US/daily_run_status.json
```

Expected: no stale daily-premarket process remains; the new status is not failed; no fresh rate-limit log appears after the dry-run start time.

- [ ] **Step 9: Run the final acceptance gate**

Run:

```bash
make acceptance
```

Expected: final line/status is `PASS`. Treat `FAIL` as work to fix and `BLOCKED` as blocked; neither is completion.
