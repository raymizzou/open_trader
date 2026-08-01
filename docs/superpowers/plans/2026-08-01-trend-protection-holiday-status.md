# Trend Protection Holiday Status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop a clean non-trading-day protection result from falsely blocking a trend controller.

**Architecture:** Keep the existing controller and watcher flow unchanged. Broaden the shared `_protection_blocker` success-status set to include `holiday`, while retaining the existing zero-exception and zero-unknown-quote checks.

**Tech Stack:** Python 3.12, pytest, launchd, Make.

## Global Constraints

- Do not change polling cadence, market-calendar derivation, report generation, or order execution.
- `holiday` is non-blocking only when both diagnostic counts are integers equal to zero.
- Final completion requires `make acceptance` to return `PASS` on the exact deployed SHA.

---

### Task 1: Fix the shared protection-result gate

**Files:**
- Modify: `tests/test_trend_market_controller.py`
- Modify: `src/open_trader/trend_market_controller.py:825-846`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: a watcher result exposing `status`, `exception_count`, and `unknown_quote_count`.
- Produces: `_protection_blocker(result: object) -> str | None` with clean `holiday` results returning `None`.

- [ ] **Step 1: Write the failing regression test**

```python
def test_protection_blocker_accepts_only_clean_holiday() -> None:
    clean = SimpleNamespace(
        status="holiday", exception_count=0, unknown_quote_count=0
    )
    unknown = SimpleNamespace(
        status="holiday", exception_count=0, unknown_quote_count=1
    )

    assert controller._protection_blocker(clean) is None
    assert controller._protection_blocker(unknown) == (
        "protection pass abnormal: status=holiday, exceptions=0, "
        "unknown_quotes=1"
    )
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_trend_market_controller.py::test_protection_blocker_accepts_only_clean_holiday -q
```

Expected: FAIL because the clean `holiday` result currently returns an abnormal-status string.

- [ ] **Step 3: Implement the one-line status change**

```python
if (
    status not in {"completed", "holiday"}
    or not isinstance(exceptions, int)
    or isinstance(exceptions, bool)
    or exceptions
    or not isinstance(unknown_quotes, int)
    or isinstance(unknown_quotes, bool)
    or unknown_quotes
):
```

- [ ] **Step 4: Verify GREEN and the controller file**

Run:

```bash
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_trend_market_controller.py::test_protection_blocker_accepts_only_clean_holiday -q
PYTHONSAFEPATH=1 PYTHONPATH="$PWD:$PWD/src" .venv/bin/python -m pytest \
  tests/test_trend_market_controller.py -q
```

Expected: both commands PASS.

- [ ] **Step 5: Update the dated operator log and commit**

Add a 2026-08-01 entry explaining that clean holiday protection passes no longer block the CN controller, while diagnostic failures remain fail-closed.

```bash
git add CHANGELOG.md src/open_trader/trend_market_controller.py \
  tests/test_trend_market_controller.py
git commit -m "fix: accept clean holiday protection passes"
```

### Task 2: Re-accept, merge, and redeploy the exact SHA

**Files:**
- Runtime only: launchd plists, ignored logs, shared `data/` status files.

**Interfaces:**
- Consumes: the committed candidate SHA and shared runtime data under `/Users/ray/projects/open_trader`.
- Produces: `main` at the accepted SHA, plus Dashboard/account-sync/CN/HK/US processes running that SHA.

- [ ] **Step 1: Restart the affected long-running processes**

```bash
scripts/install_account_sync_launchd.sh --repo-root "$PWD" \
  --runtime-root /Users/ray/projects/open_trader --python "$PWD/.venv/bin/python" \
  --wait-seconds 120
scripts/install_daily_premarket_launchd.sh --trend-only --market all \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env
scripts/install_dashboard_launchd.sh --repo-root "$PWD" \
  --runtime-root /Users/ray/projects/open_trader --python "$PWD/.venv/bin/python" \
  --wait-seconds 60
```

- [ ] **Step 2: Run the final gate**

```bash
make acceptance
```

Expected: `PASS` with the complete pytest, prediction-market, drawdown, process, API, and browser checks.

- [ ] **Step 3: Fast-forward `main` to the accepted SHA**

```bash
git -C /Users/ray/projects/open_trader merge --ff-only feat/issue-14-frontend-gateway
```

- [ ] **Step 4: Redeploy the same SHA and verify runtime facts**

Repeat the three installer commands from Step 1, then verify HTTP 200, fresh logs, and matching PID/cwd/SHA in Dashboard, account-sync, and CN/HK/US controller status.
