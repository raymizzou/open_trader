# Trend Holding Evidence Allowlist Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the seven operator-confirmed Tiger US positions to trend-holding membership when their original formal `BUY` reports are absent.

**Architecture:** Keep historical formal `BUY` artifacts as the primary provenance source. Add one source-controlled allowlist keyed by `(broker, market)` and union it only after the historical scan succeeds; both existing Dashboard surfaces continue consuming the unchanged membership contract.

**Tech Stack:** Python 3.12, pytest, existing Dashboard JSON/JavaScript, launchd acceptance workflow.

## Global Constraints

- The only allowlisted identity is `Tiger / US: AMZN, CRNX, GRMN, KO, LH, NUE, REGN`.
- Allowlist entries are normalized `MARKET.SYMBOL` values and never leak to another broker or market.
- Historical read/parse/schema failure still returns `available: false`, `symbols: []`; never publish a partial allowlist result.
- Do not infer membership from current `HOLD`, candidate, account-position, or report-presence fields.
- Do not add a Dashboard editor, database, artifact, cache, schema migration, dependency, or general override system.
- Account totals, statement reconciliation, row quantities, simulated holdings, and trading behavior remain unchanged.
- The two headings remain exactly `趋势持仓` and `非趋势持仓`; unavailable history retains `历史买入计划归属暂不可用，未执行分组`.
- Final review readiness requires unmodified `make acceptance` to return `PASS`, followed by exact-SHA redeployment and PID/cwd/SHA/log/HTTP verification.

---

### Task 1: Add the scoped historical-evidence allowlist

**Files:**
- Modify: `src/open_trader/dashboard.py:97-113,2380-2485`
- Modify: `tests/test_dashboard.py:375-550`
- Modify: `CHANGELOG.md:5-10`

**Interfaces:**
- Consumes: historical report directory `Path`, broker key `str`, market key `str`.
- Produces: `TREND_HOLDING_EVIDENCE_ALLOWLIST: dict[tuple[str, str], frozenset[str]]` and `_historical_buy_plan_membership(reports_dir: Path, *, broker: str, market: str) -> dict[str, object]`.
- Preserves: `{"available": bool, "symbols": list[str], "reason": str}` and the existing frontend join.

- [ ] **Step 1: Add RED tests for exact scope and failure behavior**

Add these real-function tests beside the existing historical-membership tests:

```python
def test_historical_buy_plan_membership_adds_tiger_us_evidence_allowlist(
    tmp_path: Path,
) -> None:
    write_buy_plan_history(
        tmp_path, "reports", "report.json", market="US", actions=[]
    )

    membership = dashboard_module._historical_buy_plan_membership(
        tmp_path / "reports", broker="tiger", market="US"
    )

    assert membership == {
        "available": True,
        "symbols": [
            "US.AMZN",
            "US.CRNX",
            "US.GRMN",
            "US.KO",
            "US.LH",
            "US.NUE",
            "US.REGN",
        ],
        "reason": "",
    }


@pytest.mark.parametrize(
    ("broker", "market"),
    [("phillips", "US"), ("tiger", "HK")],
)
def test_historical_buy_plan_membership_scopes_evidence_allowlist(
    tmp_path: Path, broker: str, market: str
) -> None:
    write_buy_plan_history(
        tmp_path, "reports", "report.json", market=market, actions=[]
    )

    membership = dashboard_module._historical_buy_plan_membership(
        tmp_path / "reports", broker=broker, market=market
    )

    assert membership == {"available": True, "symbols": [], "reason": ""}


def test_historical_buy_plan_membership_does_not_publish_partial_allowlist(
    tmp_path: Path,
) -> None:
    membership = dashboard_module._historical_buy_plan_membership(
        tmp_path / "missing", broker="tiger", market="US"
    )

    assert membership == {
        "available": False,
        "symbols": [],
        "reason": "历史趋势报告不存在",
    }
```

Update every existing direct call in `tests/test_dashboard.py` to pass `broker="test"`, so old generic history tests remain independent of the production allowlist.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_dashboard.py \
  -k 'historical_buy_plan_membership_adds_tiger_us_evidence_allowlist or historical_buy_plan_membership_scopes_evidence_allowlist or historical_buy_plan_membership_does_not_publish_partial_allowlist'
```

Expected: FAIL because `_historical_buy_plan_membership` does not yet accept `broker` and does not add the seven symbols.

- [ ] **Step 3: Add the minimal allowlist at the backend provenance seam**

Add beside `TREND_REPORT_SOURCES`:

```python
TREND_HOLDING_EVIDENCE_ALLOWLIST = {
    ("tiger", "US"): frozenset({
        "US.AMZN",
        "US.CRNX",
        "US.GRMN",
        "US.KO",
        "US.LH",
        "US.NUE",
        "US.REGN",
    }),
}
```

Change the scanner signature and successful return path only:

```python
def _historical_buy_plan_membership(
    reports_dir: Path, *, broker: str, market: str
) -> dict[str, object]:
    # existing fail-closed scan remains unchanged
    symbols.update(TREND_HOLDING_EVIDENCE_ALLOWLIST.get((broker, market), ()))
    return {"available": True, "symbols": sorted(symbols), "reason": ""}
```

Pass the already-available `broker` argument from both production callers in `_load_broker_trend_report` and `_project_broker_trend_report`. Update all existing direct tests with `broker="test"`. Do not change JavaScript or the membership payload shape.

- [ ] **Step 4: Run focused GREEN and Dashboard regressions**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_dashboard.py \
  -k 'historical_buy_plan_membership'

PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -m pytest -q \
  tests/test_dashboard.py tests/test_dashboard_acceptance.py tests/test_dashboard_web.py
```

Expected: all selected tests PASS; only the existing `websockets.legacy` deprecation warning may remain.

- [ ] **Step 5: Check the real retained Tiger history through the amended scanner**

Run:

```bash
PYTHONPATH=src /Users/ray/projects/open_trader/.venv/bin/python -c '
from pathlib import Path
from open_trader.dashboard import _historical_buy_plan_membership
required = {"US.AMZN", "US.CRNX", "US.GRMN", "US.KO", "US.LH", "US.NUE", "US.REGN"}
result = _historical_buy_plan_membership(
    Path("/Users/ray/projects/open_trader/reports/trend_us_tiger"),
    broker="tiger",
    market="US",
)
missing = sorted(required - set(result["symbols"]))
print({"available": result["available"], "missing": missing})
raise SystemExit(not (result["available"] is True and not missing))
'
```

Expected: `{'available': True, 'missing': []}` and exit 0.

- [ ] **Step 6: Update the existing dated operator changelog entry**

Amend the 2026-08-12 Dashboard bullet to state that Tiger US historical-evidence gaps for `AMZN、CRNX、GRMN、KO、LH、NUE、REGN` are补录 through a source-controlled allowlist. Do not add a duplicate date or a second feature entry.

- [ ] **Step 7: Commit the implementation**

Run:

```bash
git diff --check
git add src/open_trader/dashboard.py tests/test_dashboard.py CHANGELOG.md
git commit -m "fix: classify confirmed Tiger trend holdings"
```

- [ ] **Step 8: Run the final Dashboard acceptance gate**

From a clean committed worktree, run exactly:

```bash
make acceptance
```

Expected: full test suite, live checks, three-market Trend preflight, and Dashboard browser validator finish with `status: PASS`, `errors: []`, and `blocker: null`. On `FAIL`, diagnose and fix before rerunning; on `BLOCKED`, report the blocker without substituting partial checks.

- [ ] **Step 9: Redeploy the exact accepted SHA and verify live classification**

After PASS and without source/data changes, run the official installers:

```bash
scripts/install_account_release.sh \
  --repo-root /Users/ray/projects/open_trader/.worktrees/trend-holding-sections \
  --runtime-root /Users/ray/projects/open_trader \
  --python /Users/ray/projects/open_trader/.venv/bin/python

scripts/install_dashboard_launchd.sh --mode stack \
  --repo-root /Users/ray/projects/open_trader/.worktrees/trend-holding-sections \
  --runtime-root /Users/ray/projects/open_trader \
  --python /Users/ray/projects/open_trader/.venv/bin/python

scripts/install_daily_premarket_launchd.sh \
  --config /Users/ray/projects/open_trader/config/daily_premarket.env \
  --trend-only --market all
```

Verify Gateway, Legacy, Account API/Worker, and CN/HK/US controller PID, working directory, exact Git SHA, clean source state, fresh logs, and advancing heartbeats. Verify `http://127.0.0.1:8766/` returns HTTP 200 and `/api/dashboard` is valid JSON whose Tiger membership contains all seven required keys. Confirm both real-holding surfaces render those rows under `趋势持仓` and not `非趋势持仓`.
